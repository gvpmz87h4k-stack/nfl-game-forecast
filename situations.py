"""Situational factors: things you know on Tuesday that are about the week, not the roster.

Each factor is a yes/no fact per team from the schedule and last week's result:
  bye        the team is coming off its bye week (rest of 13 days or more)
  short      the team is on a short week (rest of 5 days or fewer)
  blowout    the team lost its last game by 17 or more
  lookahead  the team is a 7-point favorite this week and has a harder game next week
Plus one fact per game:
  divisional the two teams are in the same division (they meet twice a year)

The test asks one question of each: after the closing line has said its piece, does the
factor move the actual margin? Fit on 2022-2024, checked on 2025. Anything that does not
hold on 2025 is not added to the model, and the readout never mentions it.
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

from ratings import PBP_DIR, GAMES_URL, ols

ROOT = Path(__file__).resolve().parent


def load_schedule(seasons):
    """Regular-season games from nflverse games.csv, including future weeks (no scores yet)."""
    path = PBP_DIR / "games.csv"
    if not path.exists():
        import urllib.request
        request = urllib.request.Request(GAMES_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as handle:
            handle.write(response.read())
    games = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["game_type"] != "REG" or int(row["season"]) not in seasons:
                continue
            games.append({
                "game_id": row["game_id"], "season": int(row["season"]), "week": int(row["week"]),
                "home": row["home_team"], "away": row["away_team"],
                "spread": float(row["spread_line"]) if row["spread_line"] else None,
                "home_score": int(row["home_score"]) if row["home_score"] else None,
                "away_score": int(row["away_score"]) if row["away_score"] else None,
                "home_rest": int(row["home_rest"]) if row["home_rest"] else 7,
                "away_rest": int(row["away_rest"]) if row["away_rest"] else 7,
                "divisional": row["div_game"] == "1",
            })
    games.sort(key=lambda g: (g["season"], g["week"], g["game_id"]))
    return games


def flags(games):
    """Per game: home-minus-away differences of each team factor, plus the divisional flag."""
    last_margin = {}          # team -> margin of its previous game this season
    next_game = defaultdict(dict)  # (season, team) -> {week: game}
    for g in games:
        next_game[(g["season"], g["home"])][g["week"]] = g
        next_game[(g["season"], g["away"])][g["week"]] = g
    out = []
    season_seen = None
    for g in games:
        if g["season"] != season_seen:
            last_margin = {}
            season_seen = g["season"]
        def team_flags(team, rest, favored_by):
            f = {"bye": 1 if rest >= 13 else 0, "short": 1 if rest <= 5 else 0}
            prev = last_margin.get(team)
            f["blowout"] = 1 if prev is not None and prev <= -17 else 0
            harder_next = 0
            if favored_by is not None and favored_by >= 7:
                weeks = next_game[(g["season"], team)]
                following = next((weeks[w] for w in sorted(weeks) if w > g["week"]), None)
                if following and following["spread"] is not None:
                    their_edge = following["spread"] if following["home"] == team else -following["spread"]
                    harder_next = 1 if their_edge < 3 else 0
            f["lookahead"] = harder_next
            return f
        spread = g["spread"]
        home_f = team_flags(g["home"], g["home_rest"], spread if spread is not None else None)
        away_f = team_flags(g["away"], g["away_rest"], -spread if spread is not None else None)
        out.append({
            **g,
            "features": {key: home_f[key] - away_f[key] for key in ("bye", "short", "blowout", "lookahead")},
            "homeFlags": home_f, "awayFlags": away_f,
        })
        if g["home_score"] is not None:
            margin = g["home_score"] - g["away_score"]
            last_margin[g["home"]] = margin
            last_margin[g["away"]] = -margin
    return out


FACTORS = ("divisional", "bye", "short", "blowout", "lookahead")


def rows_for(games):
    rows = []
    for g in games:
        if g["spread"] is None or g["home_score"] is None:
            continue
        x = [1 if g["divisional"] else 0] + [g["features"][k] for k in FACTORS[1:]]
        rows.append((x, g["home_score"] - g["away_score"] - g["spread"], g))
    return rows


def evaluate(train_rows, test_rows):
    """Residual regression: does each factor move the actual margin once the line is known?"""
    coefficients = ols([x for x, _, _ in train_rows], [y for _, y, _ in train_rows])
    # divisional has no direction, so its effect is on closeness: measure |residual| separately.
    report = {"trainGames": len(train_rows), "testGames": len(test_rows), "factors": {}}
    for index, name in enumerate(FACTORS):
        coef = coefficients[index + 1]
        if name == "divisional":
            def abs_residual(rows, flag):
                vals = [abs(y) for x, y, _ in rows if x[0] == flag]
                return round(sum(vals) / len(vals), 2) if vals else None
            report["factors"][name] = {
                "trainCoefficientPoints": round(coef, 2),
                "trainAbsMissDivisional": abs_residual(train_rows, 1), "trainAbsMissOther": abs_residual(train_rows, 0),
                "testAbsMissDivisional": abs_residual(test_rows, 1), "testAbsMissOther": abs_residual(test_rows, 0),
                "testGamesFlagged": sum(1 for x, _, _ in test_rows if x[0] == 1),
            }
            continue
        # directional factors: on the test season, did the flagged side beat the line more often than not?
        def hit_rate(rows):
            flagged = [(x, y) for x, y, _ in rows if x[index] != 0]
            if not flagged:
                return None, 0
            # the coefficient's sign predicts the residual's sign for the flagged side
            hits = sum(1 for x, y in flagged if y != 0 and (y > 0) == ((x[index] * coef) > 0))
            decided = sum(1 for x, y in flagged if y != 0)
            return (round(hits / decided, 3) if decided else None), len(flagged)
        train_hit, train_n = hit_rate(train_rows)
        test_hit, test_n = hit_rate(test_rows)
        report["factors"][name] = {
            "trainCoefficientPoints": round(coef, 2),
            "trainFlagged": train_n, "trainBeatLineRate": train_hit,
            "testFlagged": test_n, "testBeatLineRate": test_hit,
        }
    report["coefficients"] = {name: round(coefficients[i + 1], 3) for i, name in enumerate(FACTORS)}
    report["intercept"] = round(coefficients[0], 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", type=int, default=2025)
    parser.add_argument("--train-from", type=int, default=2022)
    args = parser.parse_args()
    seasons = set(range(args.train_from, args.test + 1))
    games = flags(load_schedule(seasons))
    train = rows_for([g for g in games if g["season"] < args.test])
    test = rows_for([g for g in games if g["season"] == args.test])
    report = evaluate(train, test)
    out = ROOT / "data" / f"situations-{args.test}.json"
    with out.open("w") as handle:
        json.dump(report, handle, indent=2)
    print(f"Situational factors, fit {args.train_from}-{args.test - 1} ({report['trainGames']} games), checked on {args.test} ({report['testGames']} games)")
    for name, block in report["factors"].items():
        if name == "divisional":
            print(f"  divisional   effect on closeness: avg miss {block['trainAbsMissDivisional']} vs {block['trainAbsMissOther']} (fit), "
                  f"{block['testAbsMissDivisional']} vs {block['testAbsMissOther']} ({args.test}, {block['testGamesFlagged']} games)")
        else:
            print(f"  {name:11s}  {block['trainCoefficientPoints']:+.2f} pts (fit, {block['trainFlagged']} games, beat line {block['trainBeatLineRate']}) | "
                  f"{args.test}: {block['testFlagged']} games, beat line {block['testBeatLineRate']}")


if __name__ == "__main__":
    main()
