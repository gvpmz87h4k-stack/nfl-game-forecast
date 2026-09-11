"""Does travel move the result once the closing line is known? Tested the way the situational
factors were: fit on 2022-2024, checked blind on 2025, nothing hand-entered.

Per team and game, from the schedule and stadium locations alone:
  miles       great-circle distance from the team's home stadium to the venue (home team at home: 0)
  zones       time zones crossed, in hours, absolute
  bodyClock   the kickoff hour on the team's home body clock (a West Coast team at a 1 PM Eastern
              kickoff reads 10 AM)
  abroad      the venue is outside the United States
Features are home minus away, so a positive coefficient means the effect favors the home team.
The target is the residual: actual home margin minus the closing spread (home favored positive).
"""

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ratings import PBP_DIR, ols

ROOT = Path(__file__).resolve().parent

# Home stadiums: latitude, longitude, time zone. nflverse team codes.
HOME = {
    "ARI": (33.5276, -112.2626, "America/Phoenix"), "ATL": (33.7554, -84.4010, "America/New_York"),
    "BAL": (39.2780, -76.6227, "America/New_York"), "BUF": (42.7738, -78.7870, "America/New_York"),
    "CAR": (35.2258, -80.8528, "America/New_York"), "CHI": (41.8623, -87.6167, "America/Chicago"),
    "CIN": (39.0955, -84.5161, "America/New_York"), "CLE": (41.5061, -81.6995, "America/New_York"),
    "DAL": (32.7473, -97.0945, "America/Chicago"), "DEN": (39.7439, -105.0201, "America/Denver"),
    "DET": (42.3400, -83.0456, "America/Detroit"), "GB": (44.5013, -88.0622, "America/Chicago"),
    "HOU": (29.6847, -95.4107, "America/Chicago"), "IND": (39.7601, -86.1639, "America/Indiana/Indianapolis"),
    "JAX": (30.3240, -81.6373, "America/New_York"), "KC": (39.0489, -94.4839, "America/Chicago"),
    "LA": (33.9535, -118.3392, "America/Los_Angeles"), "LAC": (33.9535, -118.3392, "America/Los_Angeles"),
    "LV": (36.0909, -115.1833, "America/Los_Angeles"), "MIA": (25.9580, -80.2389, "America/New_York"),
    "MIN": (44.9735, -93.2575, "America/Chicago"), "NE": (42.0909, -71.2643, "America/New_York"),
    "NO": (29.9511, -90.0812, "America/Chicago"), "NYG": (40.8135, -74.0745, "America/New_York"),
    "NYJ": (40.8135, -74.0745, "America/New_York"), "PHI": (39.9008, -75.1675, "America/New_York"),
    "PIT": (40.4468, -80.0158, "America/New_York"), "SEA": (47.5952, -122.3316, "America/Los_Angeles"),
    "SF": (37.4030, -121.9700, "America/Los_Angeles"), "TB": (27.9759, -82.5033, "America/New_York"),
    "TEN": (36.1665, -86.7713, "America/Chicago"), "WAS": (38.9076, -76.8645, "America/New_York"),
}
# Neutral venues that are not a team's home, by nflverse stadium name.
VENUES = {
    "Tottenham Stadium": (51.6043, -0.0664, "Europe/London", True), "Wembley Stadium": (51.5560, -0.2795, "Europe/London", True),
    "Allianz Arena": (48.2188, 11.6247, "Europe/Berlin", True), "Deutsche Bank Park": (50.0686, 8.6455, "Europe/Berlin", True),
    "Azteca Stadium": (19.3029, -99.1505, "America/Mexico_City", True), "Arena Corinthians": (-23.5453, -46.4742, "America/Sao_Paulo", True),
    "Estadio Santiago Bernabeu": (40.4531, -3.6883, "Europe/Madrid", True), "Croke Park": (53.3607, -6.2511, "Europe/Dublin", True),
    "Melbourne Cricket Ground": (-37.8200, 144.9834, "Australia/Melbourne", True),
}
STADIUM_OF = {  # neutral games played in an NFL stadium: which home table entry to use
    "Acrisure Stadium": "PIT", "FirstEnergy Stadium": "CLE", "Ford Field": "DET", "Hard Rock Stadium": "MIA",
    "Lucas Oil Stadium": "IND", "MetLife Stadium": "NYG", "SoFi Stadium": "LA", "TIAA Bank Stadium": "JAX",
}


def miles(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def venue_for(row):
    if row["location"] == "Home":
        lat, lon, tz = HOME[row["home_team"]]
        return (lat, lon, tz, False)
    if row["stadium"] in VENUES:
        return VENUES[row["stadium"]]
    if row["stadium"] in STADIUM_OF:
        lat, lon, tz = HOME[STADIUM_OF[row["stadium"]]]
        return (lat, lon, tz, False)
    lat, lon, tz = HOME[row["home_team"]]
    return (lat, lon, tz, False)


def team_features(team, row, venue):
    lat, lon, tz = HOME[team]
    vlat, vlon, vtz, abroad = venue
    kickoff_et = datetime.fromisoformat(f"{row['gameday']} {row['gametime']}").replace(tzinfo=ZoneInfo("America/New_York"))
    venue_offset = kickoff_et.astimezone(ZoneInfo(vtz)).utcoffset().total_seconds() / 3600
    home_offset = kickoff_et.astimezone(ZoneInfo(tz)).utcoffset().total_seconds() / 3600
    body = kickoff_et.astimezone(ZoneInfo(tz))
    return {
        "miles": miles((lat, lon), (vlat, vlon)) / 1000.0,
        "zones": abs(venue_offset - home_offset),
        "bodyClock": body.hour + body.minute / 60.0,
        "abroad": 1.0 if abroad else 0.0,
    }


FACTORS = ("miles", "zones", "bodyClock", "abroad")


def load_rows(seasons):
    rows = []
    with (PBP_DIR / "games.csv").open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["game_type"] != "REG" or int(row["season"]) not in seasons:
                continue
            if not row["spread_line"] or not row["home_score"] or not row["gametime"]:
                continue
            venue = venue_for(row)
            h = team_features(row["home_team"], row, venue)
            a = team_features(row["away_team"], row, venue)
            x = [h[k] - a[k] for k in FACTORS]
            y = (int(row["home_score"]) - int(row["away_score"])) - float(row["spread_line"])
            rows.append((x, y, row, h, a))
    return rows


def beat_rate(rows, index, coef, threshold):
    """Among games where the factor difference is at least `threshold` in size, how often did
    the side the coefficient points to beat the closing line?"""
    picked = [(x, y) for x, y, *_ in rows if abs(x[index]) >= threshold and y != 0]
    if not picked:
        return None, 0
    hits = sum(1 for x, y in picked if (y > 0) == ((x[index] * coef) > 0))
    return round(hits / len(picked), 3), len(picked)


def main():
    train = load_rows({2022, 2023, 2024})
    test = load_rows({2025})
    coefficients = ols([x for x, *_ in train], [y for _, y, *_ in train])
    print(f"Travel factors, fit 2022-2024 ({len(train)} games), checked on 2025 ({len(test)} games)")
    print(f"  intercept {coefficients[0]:+.2f} points")
    thresholds = {"miles": 1.0, "zones": 2.0, "bodyClock": 2.0, "abroad": 0.5}
    report = {"trainGames": len(train), "testGames": len(test), "factors": {}}
    for i, name in enumerate(FACTORS):
        coef = coefficients[i + 1]
        tr, trn = beat_rate(train, i, coef, thresholds[name])
        te, ten = beat_rate(test, i, coef, thresholds[name])
        report["factors"][name] = {"coefficientPoints": round(coef, 3), "threshold": thresholds[name],
                                   "trainFlagged": trn, "trainBeatLineRate": tr, "testFlagged": ten, "testBeatLineRate": te}
        print(f"  {name:10s} {coef:+.3f} pts per unit | fit: {trn} games with |diff|>={thresholds[name]}, beat line {tr} | 2025: {ten} games, beat line {te}")
    # single-factor sanity checks nobody can argue with
    def single(rows, pick, label):
        chosen = [(y) for x, y, row, h, a in rows if pick(h, a, row) and y != 0]
        if chosen:
            print(f"  {label}: {len(chosen)} games, home side beat the line {round(sum(1 for y in chosen if y > 0) / len(chosen), 3)}")
    for rows, tag in ((train, "fit"), (test, "2025")):
        single(rows, lambda h, a, r: a["zones"] >= 3, f"[{tag}] away team crossed 3+ zones")
        single(rows, lambda h, a, r: a["bodyClock"] <= 10.5 and a["zones"] >= 2, f"[{tag}] away team body clock 10:30 AM or earlier")
        single(rows, lambda h, a, r: h["abroad"] == 1, f"[{tag}] played abroad (home side = listed home)")
    (ROOT / "data" / "travel-test-2025.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
