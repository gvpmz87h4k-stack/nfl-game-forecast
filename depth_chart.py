"""Projected starting quarterbacks, read from ESPN's depth charts on every run.

The rating model's quarterback term asks "what if a different man starts", and until now a
person had to type the answer into overrides.json. ESPN publishes a depth chart per team,
the same feed family the injury list comes from, and the first quarterback on it is the
projected starter. This reads all 32, maps each name onto the play-by-play table so the
rating can use his own numbers, and says plainly when a starter is new or has no plays yet.

Manual entries in overrides.json still win, for the day the depth chart is wrong.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEPTH_CHART = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/depthcharts"
CACHE = ROOT / "data" / "depth-charts.json"
SUFFIXES = ("jr", "sr", "ii", "iii", "iv", "v")


def normalize(name):
    """First initial plus last name, so 'M. Penix Jr.', 'M.Penix', 'C.J. Stroud' and 'C.Stroud' compare
    the way the play-by-play table spells them."""
    parts = re.sub(r"[.\-']", " ", name or "").lower().split()
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    if not parts:
        return ""
    return parts[0][0] + parts[-1]


def starters_on_chart(raw):
    """Every player listed first at his slot on any formation of the depth chart."""
    names = set()
    for group in raw.get("depthchart", []):
        for slot, position in (group.get("positions") or {}).items():
            athletes = position.get("athletes") or []
            if athletes and athletes[0].get("displayName"):
                names.add(athletes[0]["displayName"])
    return sorted(names)


def everyone_on_chart(raw):
    """Every name that appears anywhere on the depth chart, any rank."""
    names = set()
    for group in raw.get("depthchart", []):
        for slot, position in (group.get("positions") or {}).items():
            for athlete in position.get("athletes") or []:
                if athlete.get("displayName"):
                    names.add(athlete["displayName"])
    return sorted(names)


def top_quarterback(raw):
    """The first quarterback on a team's depth chart, or None if the feed has no QB slot."""
    for group in raw.get("depthchart", []):
        for slot, position in (group.get("positions") or {}).items():
            if slot.lower() != "qb":
                continue
            athletes = position.get("athletes") or []
            if not athletes:
                return None
            athlete = athletes[0]
            statuses = []
            for injury in athlete.get("injuries") or []:
                status = injury.get("status") or (injury.get("type") or {}).get("description")
                if status:
                    statuses.append(status)
            return {
                "espnId": str(athlete.get("id", "")),
                "name": athlete.get("displayName", ""),
                "shortName": athlete.get("shortName", ""),
                "status": statuses[0] if statuses else None,
            }
    return None


def match_rating_name(short_name, qb_names):
    """The nflverse spelling of this quarterback, if his plays are in the rating table."""
    wanted = normalize(short_name)
    if not wanted:
        return None
    for candidate in qb_names:
        if normalize(candidate) == wanted:
            return candidate
    return None


def fetch_starters(team_id_to_abbr, get_json, qb_names):
    """{abbr: {name, shortName, espnId, status, ratingName, source}} for every team that answered."""
    starters = {}
    problems = []
    for team_id, abbr in sorted(team_id_to_abbr.items(), key=lambda item: item[1]):
        try:
            raw = get_json(DEPTH_CHART.format(team_id=team_id))
        except Exception as exc:  # one bad team must not sink the run
            problems.append(f"{abbr}: {exc}")
            continue
        top = top_quarterback(raw)
        if not top:
            problems.append(f"{abbr}: no quarterback slot on the depth chart")
            continue
        top["ratingName"] = match_rating_name(top["shortName"], qb_names)
        top["source"] = "ESPN depth chart"
        top["lineup"] = starters_on_chart(raw)
        top["chartAll"] = everyone_on_chart(raw)
        starters[abbr] = top
    return starters, problems


def load_cached():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save(starters, problems):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "teams": starters, "problems": problems,
    }
    CACHE.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def projected_starters(team_id_to_abbr, get_json, qb_names, manual=None):
    """Depth-chart starters with manual overrides on top. Falls back to the last saved chart per team."""
    starters, problems = fetch_starters(team_id_to_abbr, get_json, qb_names)
    cached = load_cached().get("teams", {})
    for abbr, entry in cached.items():
        if abbr not in starters and abbr in team_id_to_abbr.values():
            starters[abbr] = {**entry, "source": "ESPN depth chart, last saved copy"}
    for abbr, name in (manual or {}).items():
        starters[abbr] = {
            "name": name, "shortName": name, "espnId": None, "status": None,
            "ratingName": match_rating_name(name, qb_names) or name, "source": "overrides.json",
            "lineup": (starters.get(abbr) or {}).get("lineup", []),
            "chartAll": (starters.get(abbr) or {}).get("chartAll", []),
        }
    save(starters, problems)
    return starters, problems
