"""Fetch the CBS Sports expert picks for a week and write them into picks.json.

CBS renders its expert picks table in plain HTML on two pages, straight up and against the
spread, one row per game and one column per writer. This reads both, records every writer's
pick under their own source key, and adds a "cbs" consensus (a strict majority of the
writers who have picked; a tie records nothing). The updater then freezes the picks with
the forecast at kickoff and the ledger grades them like everything else.

If the page changes shape and fewer than half the week's games parse, nothing is written
and the run says so, because a scraper that quietly finds nothing looks like "no picks".
"""

import argparse
import html
import json
import re
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKS = ROOT / "picks.json"
SNAPSHOT = ROOT / "data" / "snapshot.json"
PAGES = {
    "winner": "https://www.cbssports.com/nfl/picks/experts/straight-up/{week}/",
    "spread": "https://www.cbssports.com/nfl/picks/experts/against-the-spread/{week}/",
}
CBS_TO_ESPN = {"JAC": "JAX", "WAS": "WSH"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "ignore")


def _text(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_table(page):
    """Return (experts, games). games: list of {away, home, date, picks: [team or None per expert]}."""
    start = page.find("TableExpertPicks")
    if start < 0:
        return [], []
    end = page.find("</table>", start)
    table = page[start:end]
    head = table[:table.find("</thead>")]
    experts = []
    for cell in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S):
        words = _text(cell).split()
        if len(words) >= 2 and words[0][:1].isupper() and words[1][:1].isupper():
            experts.append(f"{words[0]} {words[1]}")
    body = table[table.find("<tbody"):]
    games = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        match = re.search(r"NFL_(\d{8})_([A-Z]{2,3})@([A-Z]{2,3})", row)
        if not match:
            continue
        date, away, home = match.groups()
        away, home = CBS_TO_ESPN.get(away, away), CBS_TO_ESPN.get(home, home)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)[1:]
        picks = []
        for cell in cells:
            pick = re.search(r"\b([A-Z]{2,3})\b", _text(cell))
            team = CBS_TO_ESPN.get(pick.group(1), pick.group(1)) if pick else None
            picks.append(team if team in (away, home) else None)
        games.append({"date": date, "away": away, "home": home, "picks": picks})
    return experts, games


def consensus(picks):
    votes = Counter(p for p in picks if p)
    if not votes:
        return None
    (top, n), *rest = votes.most_common(2)
    if rest and rest[0][1] == n:
        return None
    return top


def slug(name):
    return "cbs-" + re.sub(r"[^a-z]", "", name.split()[-1].lower())


def build_entries(week, tables):
    """tables: {"winner": (experts, games), "spread": (experts, games)} -> {source_key: {label, picks}}"""
    sources = {"cbs": {"label": "CBS Sports experts, consensus", "picks": []}}
    by_game = {}
    for kind, (experts, games) in tables.items():
        for game in games:
            key = (game["away"], game["home"])
            entry = by_game.setdefault(key, {"week": week, "away": game["away"], "home": game["home"]})
            entry[kind] = consensus(game["picks"])
            for expert, pick in zip(experts, game["picks"]):
                src = sources.setdefault(slug(expert), {"label": f"CBS Sports, {expert}", "picks": []})
                per = next((p for p in src["picks"] if p["away"] == game["away"] and p["home"] == game["home"]), None)
                if per is None:
                    per = {"week": week, "away": game["away"], "home": game["home"]}
                    src["picks"].append(per)
                if pick:
                    per[kind] = pick
    for entry in by_game.values():
        if entry.get("winner") or entry.get("spread"):
            sources["cbs"]["picks"].append(entry)
    for src in sources.values():
        src["picks"] = [p for p in src["picks"] if p.get("winner") or p.get("spread")]
    return {key: src for key, src in sources.items() if src["picks"] or key == "cbs"}


def merge(existing, week, sources):
    """Replace this week's CBS entries; keep other weeks and any non-CBS sources untouched."""
    out = dict(existing)
    out.setdefault("sources", {})
    for key, src in sources.items():
        current = out["sources"].get(key, {"label": src["label"], "picks": []})
        kept = [p for p in current.get("picks", []) if p.get("week") != week]
        current["label"] = src["label"]
        current["picks"] = kept + src["picks"]
        out["sources"][key] = current
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, help="Defaults to the snapshot's current week")
    args = parser.parse_args()
    week = args.week
    if week is None:
        with SNAPSHOT.open() as handle:
            week = json.load(handle)["currentWeek"]
    tables = {}
    for kind, url in PAGES.items():
        try:
            tables[kind] = parse_table(fetch(url.format(week=week)))
        except Exception as exc:  # network or shape failure: say so, change nothing
            print(f"CBS picks: could not read {kind} page for week {week}: {exc}")
            return
    games = max(len(tables[k][1]) for k in tables)
    picked = sum(1 for k in tables for g in tables[k][1] if any(g["picks"]))
    if games < 8 or picked == 0:
        print(f"CBS picks: week {week} page parsed {games} games with {picked} picked rows; leaving picks.json unchanged.")
        return
    sources = build_entries(week, tables)
    existing = json.loads(PICKS.read_text()) if PICKS.exists() else {"sources": {}}
    merged = merge(existing, week, sources)
    PICKS.write_text(json.dumps(merged, indent=2) + "\n")
    cons = sources["cbs"]["picks"]
    print(f"CBS picks: week {week}, {games} games, {len(tables['winner'][0])} experts, consensus on {len(cons)} games "
          f"({sum(1 for p in cons if p.get('winner'))} straight up, {sum(1 for p in cons if p.get('spread'))} against the spread).")


if __name__ == "__main__":
    main()
