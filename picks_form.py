"""Collect picks submitted from the app (a Netlify form named "picks") into picks.json.

The app posts one submission per tap: week, away, home, winner, optional spread side, and
who is picking. This reads the submissions through Netlify's API, keeps the latest one per
game and person that arrived before that game's kickoff, and writes them into picks.json
under the person's source key. Runs in the scheduled job before the forecast refresh.

Needs NETLIFY_AUTH_TOKEN in the environment and the site id below. Without the token it
does nothing and says so, so the rest of the run is unaffected.
"""

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKS = ROOT / "picks.json"
SNAPSHOT = ROOT / "data" / "snapshot.json"
SITE_ID = "8e454e87-506e-4897-a477-529240c20fea"
API = "https://api.netlify.com/api/v1"
FORM_NAME = "picks"
PEOPLE = {"narcisa": "Narcisa"}   # who may submit; anything else is ignored


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


def merge_submissions(picks, submissions, kickoff_by_game):
    """Latest pre-kickoff submission per person per game wins; later ones for the same game replace earlier ones."""
    latest = {}
    for sub in submissions:
        data = sub.get("data") or {}
        who = (data.get("who") or "").strip().lower()
        if who not in PEOPLE:
            continue
        try:
            week = int(data.get("week"))
        except (TypeError, ValueError):
            continue
        away, home = (data.get("away") or "").upper(), (data.get("home") or "").upper()
        winner, spread = (data.get("winner") or "").upper() or None, (data.get("spread") or "").upper() or None
        if winner not in (away, home) and spread not in (away, home):
            continue
        kickoff = kickoff_by_game.get((week, away, home))
        created = sub.get("created_at")
        if not kickoff or not created or _parse(created) >= _parse(kickoff):
            continue
        key = (who, week, away, home)
        if key not in latest or _parse(created) > _parse(latest[key]["created"]):
            latest[key] = {"created": created, "week": week, "away": away, "home": home, "winner": winner, "spread": spread}
    for (who, week, away, home), entry in latest.items():
        src = picks["sources"].setdefault(who, {"label": PEOPLE[who], "picks": []})
        src["picks"] = [p for p in src["picks"] if not (p["week"] == week and p["away"] == away and p["home"] == home)]
        pick = {"week": week, "away": away, "home": home}
        if entry["winner"]:
            pick["winner"] = entry["winner"]
        if entry["spread"]:
            pick["spread"] = entry["spread"]
        pick["enteredAt"] = entry["created"]
        src["picks"].append(pick)
        src["picks"].sort(key=lambda p: (p["week"], p["away"], p["home"]))
    return picks, len(latest)


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
    print(f"App picks: {len(submissions)} submissions read, {count} picks kept (latest per game, before kickoff).")


if __name__ == "__main__":
    main()
