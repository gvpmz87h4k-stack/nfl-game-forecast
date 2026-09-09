"""Collect picks submitted from the app (a Netlify form named "picks") into picks.json.

The app posts one submission per person per week: who, week, and a "picks" field holding a
JSON list of {away, home, winner, spread} for every game that person has touched that week.
An entry with neither a winner nor a spread side clears that game. Older submissions carried
one game each in plain fields; those still read. For each game the latest submission that
arrived before kickoff wins, and the result is written into picks.json under the person's
source key. Runs in the scheduled job before the forecast refresh.

Who may submit is the list in data/people.json ({key: display name}); anything else is
ignored. Needs NETLIFY_AUTH_TOKEN in the environment; without it this does nothing and says so.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKS = ROOT / "picks.json"
SNAPSHOT = ROOT / "data" / "snapshot.json"
SITE_ID = "8e454e87-506e-4897-a477-529240c20fea"
API = "https://api.netlify.com/api/v1"
FORM_NAME = "picks"
# Who may pick. The PICKERS secret is JSON: {"<code>": "Display Name", ...}. The code travels in
# the form's "who" field and never appears in any public file; picks.json is keyed by the slug
# of the display name. Anyone without a code is ignored, so nobody can pick as someone else.
LEGACY_KEYS = {"narcisa": "Narcisa"}
LEGACY_BEFORE = "2026-09-09T05:00:00Z"   # submissions made with the old plain name, before codes existed


def slug(label):
    return re.sub(r"[^a-z0-9]", "", str(label).lower()) or "picker"


def load_pickers():
    """{code: (slug, label)} from the PICKERS environment variable."""
    raw = os.environ.get("PICKERS", "")
    try:
        mapping = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        mapping = {}
    return {str(code).strip(): (slug(label), str(label)) for code, label in mapping.items() if str(code).strip()}


def api(path, token):
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}", "User-Agent": "sunday-desk"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_submissions(token):
    forms = api(f"/sites/{SITE_ID}/forms", token)
    form = next((f for f in forms if f.get("name") == FORM_NAME), None)
    if not form:
        return []
    return api(f"/forms/{form['id']}/submissions?per_page=1000", token)


def kickoffs(snapshot):
    return {(g["week"], g["away"]["abbreviation"], g["home"]["abbreviation"]): g["kickoff"] for g in snapshot["games"]}


def _parse(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _entries(data):
    """The games inside one submission: a weekly batch, or a single legacy game."""
    raw = data.get("picks")
    if raw:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    return [{"away": data.get("away"), "home": data.get("home"), "winner": data.get("winner"), "spread": data.get("spread")}]


def identify(data, created, pickers):
    """(slug, label) for a submission, or None if its code is unknown."""
    who = (data.get("who") or "").strip()
    if who in pickers:
        return pickers[who]
    key = who.lower()
    if key in LEGACY_KEYS and created < LEGACY_BEFORE:
        return key, LEGACY_KEYS[key]
    return None


def merge_submissions(picks, submissions, kickoff_by_game, pickers=None, now=None):
    """Per person and game, the latest pre-kickoff submission wins; an empty entry clears the game.

    A pick is written to picks.json only once its game has kicked off. Until then it stays in
    Netlify's form store, which needs the key to read, so nobody can see a pick before the game
    by reading the public file. The run at or after kickoff writes and freezes it."""
    pickers = pickers if pickers is not None else load_pickers()
    now = now or datetime.now(timezone.utc)
    people = {}
    latest = {}
    for sub in submissions:
        data = sub.get("data") or {}
        created = sub.get("created_at")
        if not created:
            continue
        identity = identify(data, created, pickers)
        if not identity:
            continue
        who, label = identity
        people[who] = label
        try:
            week = int(data.get("week"))
        except (TypeError, ValueError):
            continue
        for item in _entries(data):
            away, home = (item.get("away") or "").upper(), (item.get("home") or "").upper()
            winner, spread = (item.get("winner") or "").upper() or None, (item.get("spread") or "").upper() or None
            if winner not in (away, home, None) or spread not in (away, home, None):
                continue
            kickoff = kickoff_by_game.get((week, away, home))
            if not kickoff or _parse(created) >= _parse(kickoff):
                continue
            if _parse(kickoff) > now:
                continue                      # not kicked off yet: stays private until it has
            key = (who, week, away, home)
            if key not in latest or _parse(created) > _parse(latest[key]["created"]):
                latest[key] = {"created": created, "week": week, "away": away, "home": home, "winner": winner, "spread": spread}
    kept = 0
    for (who, week, away, home), entry in latest.items():
        src = picks["sources"].setdefault(who, {"label": people[who], "picks": []})
        src["label"] = people[who]
        src["picks"] = [p for p in src["picks"] if not (p["week"] == week and p["away"] == away and p["home"] == home)]
        if not entry["winner"] and not entry["spread"]:
            continue
        pick = {"week": week, "away": away, "home": home}
        if entry["winner"]:
            pick["winner"] = entry["winner"]
        if entry["spread"]:
            pick["spread"] = entry["spread"]
        pick["enteredAt"] = entry["created"]
        src["picks"].append(pick)
        src["picks"].sort(key=lambda p: (p["week"], p["away"], p["home"]))
        kept += 1
    return picks, kept


def main():
    token = os.environ.get("NETLIFY_AUTH_TOKEN")
    if not token:
        print("App picks: NETLIFY_AUTH_TOKEN not set, skipping.")
        return
    try:
        submissions = fetch_submissions(token)
    except Exception as exc:
        print(f"App picks: could not read submissions: {exc}")
        return
    snapshot = json.loads(SNAPSHOT.read_text())
    picks = json.loads(PICKS.read_text()) if PICKS.exists() else {"sources": {}}
    picks, count = merge_submissions(picks, submissions, kickoffs(snapshot))
    PICKS.write_text(json.dumps(picks, indent=2) + "\n")
    print(f"App picks: {len(submissions)} submissions read, {count} picks written for games that have kicked off (latest per person and game, entered before kickoff).")


if __name__ == "__main__":
    main()
