"""Team ratings from play-by-play data, with the quarterback as his own component.

Source: nflverse play-by-play (expected points added per play, computed by nflfastR).
Everything here is walk-forward safe: a rating "as of" a season and week uses only games
played before that week, so the same code produces the live 2026 ratings and the blind
replay of earlier seasons.

The three features per game, home minus away:
  off   opponent-adjusted offensive EPA per play
  def   opponent-adjusted defensive EPA per play allowed, sign flipped so higher is better
  qb    how much the projected starter differs from the passing the team's rating was built on
        (zero when the same quarterback keeps starting; the whole point is a regime change)
A least-squares fit turns those into a predicted margin, and a second fit blends that margin
with the market line. Both fits are learned on earlier seasons only.
"""

import argparse
import csv
import gzip
import json
import math
import os
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PBP_DIR = ROOT / "data" / "pbp"
PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
MARGIN_SD = 13.86
# Which plays feed the ratings. Regular season only by default; set RATINGS_PLAYOFFS=1 to add
# playoff plays (nflverse weeks 19 to 22, which sort after the regular season as they should).
SEASON_TYPES = {"REG", "POST"} if os.environ.get("RATINGS_PLAYOFFS") == "1" else {"REG"}

# ESPN and nflverse do not agree on three abbreviations.
NFLVERSE_TO_ESPN = {"LA": "LAR", "WAS": "WSH", "JAX": "JAX"}
ESPN_TO_NFLVERSE = {value: key for key, value in NFLVERSE_TO_ESPN.items()}


def normal_cdf(value):
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


# ----------------------------------------------------------------------------- data

def ensure_pbp(season):
    PBP_DIR.mkdir(parents=True, exist_ok=True)
    path = PBP_DIR / f"play_by_play_{season}.csv.gz"
    if path.exists() and path.stat().st_size > 1000:
        return path
    request = urllib.request.Request(PBP_URL.format(season=season), headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as handle:
            handle.write(response.read())
    except Exception:
        if path.exists():
            path.unlink()
        return None
    return path


def load_team_games(season):
    """One record per team per regular-season game, with the passer who took the most dropbacks."""
    path = ensure_pbp(season)
    if path is None:
        return []
    per = {}
    meta = {}
    with gzip.open(path, "rt", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row["season_type"] not in SEASON_TYPES or row["play_type"] not in ("pass", "run"):
                continue
            if not row["posteam"] or not row["epa"]:
                continue
            epa = float(row["epa"])
            key = (row["game_id"], row["posteam"])
            rec = per.get(key)
            if rec is None:
                rec = per[key] = {"off_epa": 0.0, "off_plays": 0, "pass_epa": 0.0, "dropbacks": 0, "qb": {}}
            rec["off_epa"] += epa
            rec["off_plays"] += 1
            if row["qb_dropback"] == "1":
                rec["pass_epa"] += epa
                rec["dropbacks"] += 1
                passer = row["passer_player_id"]
                if passer:
                    q = rec["qb"].setdefault(passer, {"name": row["passer_player_name"], "dropbacks": 0, "epa": 0.0})
                    q["dropbacks"] += 1
                    q["epa"] += epa
            if row["game_id"] not in meta:
                meta[row["game_id"]] = {
                    "season": int(row["season"]), "week": int(row["week"]),
                    "home": row["home_team"], "away": row["away_team"], "date": row["game_date"],
                }
    records = []
    for (game_id, team), rec in per.items():
        game = meta[game_id]
        opp = game["away"] if team == game["home"] else game["home"]
        opp_rec = per.get((game_id, opp), {"off_epa": 0.0, "off_plays": 0})
        starter_id, starter = None, None
        if rec["qb"]:
            starter_id, starter = max(rec["qb"].items(), key=lambda item: item[1]["dropbacks"])
        records.append({
            "season": game["season"], "week": game["week"], "game_id": game_id, "date": game["date"],
            "team": team, "opp": opp, "home": team == game["home"],
            "off_epa": rec["off_epa"], "off_plays": rec["off_plays"],
            "pass_epa": rec["pass_epa"], "dropbacks": rec["dropbacks"],
            "def_epa": opp_rec["off_epa"], "def_plays": opp_rec["off_plays"],
            "starter_id": starter_id, "starter_name": starter["name"] if starter else None,
            "starter_dropbacks": starter["dropbacks"] if starter else 0,
            "starter_epa": starter["epa"] if starter else 0.0,
            "passers": rec["qb"],
        })
    records.sort(key=lambda item: (item["date"], item["game_id"], item["team"]))
    return records


def load_market_games(seasons):
    """Regular-season games with the nflverse closing spread (positive = home favored) and scores."""
    path = PBP_DIR / "games.csv"
    if not path.exists():
        request = urllib.request.Request(GAMES_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, path.open("wb") as handle:
            handle.write(response.read())
    games = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row["game_type"] != "REG" or int(row["season"]) not in seasons:
                continue
            if not row["spread_line"] or not row["home_score"]:
                continue
            games.append({
                "game_id": row["game_id"], "season": int(row["season"]), "week": int(row["week"]),
                "home": row["home_team"], "away": row["away_team"],
                "spread": float(row["spread_line"]),
                "home_score": int(row["home_score"]), "away_score": int(row["away_score"]),
            })
    return games


# ----------------------------------------------------------------------------- ratings

# Chosen on a 2024 validation season (trained 2022-2023) by model-only Brier, before 2025 was touched.
DEFAULTS = {
    "halfLifeGames": 16.0,      # a game 16 back counts half as much as the latest one
    "seasonDiscount": 0.35,     # every full season back multiplies weight by this
    "priorPlays": 700.0,        # shrinkage toward league average, in plays
    "priorDropbacks": 80.0,     # shrinkage for a quarterback's own EPA per dropback
    "iterations": 4,            # opponent-adjustment passes
}


def _weights(records, cutoff_season, cutoff_week, params):
    """Recency weight per record, computed per team, for everything before the cutoff."""
    before = [r for r in records if (r["season"], r["week"]) < (cutoff_season, cutoff_week)]
    by_team = defaultdict(list)
    for rec in before:
        by_team[rec["team"]].append(rec)
    weighted = []
    for team, items in by_team.items():
        items.sort(key=lambda r: (r["season"], r["week"]))
        total = len(items)
        for index, rec in enumerate(items):
            games_back = total - 1 - index
            seasons_back = cutoff_season - rec["season"]
            weight = (0.5 ** (games_back / params["halfLifeGames"])) * (params["seasonDiscount"] ** seasons_back)
            weighted.append((rec, weight))
    return weighted


def compute_ratings(records, cutoff_season, cutoff_week, params=None):
    """Opponent-adjusted offense and defense per team, plus the passer table, as of the cutoff."""
    params = {**DEFAULTS, **(params or {})}
    weighted = _weights(records, cutoff_season, cutoff_week, params)
    teams = {rec["team"] for rec, _ in weighted}
    off = {team: 0.0 for team in teams}
    dfn = {team: 0.0 for team in teams}
    prior = params["priorPlays"]
    for _ in range(params["iterations"]):
        off_num = defaultdict(float); off_den = defaultdict(float)
        def_num = defaultdict(float); def_den = defaultdict(float)
        for rec, weight in weighted:
            opp = rec["opp"]
            off_num[rec["team"]] += weight * (rec["off_epa"] - rec["off_plays"] * dfn.get(opp, 0.0))
            off_den[rec["team"]] += weight * rec["off_plays"]
            def_num[rec["team"]] += weight * (rec["def_epa"] - rec["def_plays"] * off.get(opp, 0.0))
            def_den[rec["team"]] += weight * rec["def_plays"]
        off = {team: off_num[team] / (off_den[team] + prior) for team in teams}
        dfn = {team: def_num[team] / (def_den[team] + prior) for team in teams}

    # Passing produced the team rating; the quarterback table lets us ask "what if the starter changes".
    team_pass_num = defaultdict(float); team_pass_den = defaultdict(float)
    qb_num = defaultdict(float); qb_den = defaultdict(float); qb_name = {}
    for rec, weight in weighted:
        team_pass_num[rec["team"]] += weight * rec["pass_epa"]
        team_pass_den[rec["team"]] += weight * rec["dropbacks"]
        for passer_id, q in rec["passers"].items():
            qb_num[passer_id] += weight * q["epa"]
            qb_den[passer_id] += weight * q["dropbacks"]
            qb_name[passer_id] = q["name"]
    team_pass = {team: team_pass_num[team] / (team_pass_den[team] + prior) for team in teams}
    qb = {pid: qb_num[pid] / (qb_den[pid] + params["priorDropbacks"]) for pid in qb_num}

    # Projected starter: most dropbacks across the team's last four games, so a rested Week 18
    # or a one-game injury fill-in does not become the "starter" for the next game.
    last_starter = {}
    by_team = defaultdict(list)
    for rec, _ in weighted:
        by_team[rec["team"]].append(rec)
    for team, items in by_team.items():
        recent = sorted(items, key=lambda r: (r["season"], r["week"]))[-4:]
        tally = defaultdict(int)
        for rec in recent:
            for passer_id, q in rec["passers"].items():
                tally[passer_id] += q["dropbacks"]
        if tally:
            last_starter[team] = max(tally.items(), key=lambda item: item[1])[0]
    return {
        "cutoff": {"season": cutoff_season, "week": cutoff_week},
        "offense": off, "defense": dfn, "teamPass": team_pass,
        "qb": qb, "qbName": qb_name, "lastStarter": last_starter,
    }


def game_features(ratings, home, away, home_starter=None, away_starter=None):
    """Home-minus-away features. Starters default to whoever started each team's last game."""
    home_starter = home_starter or ratings["lastStarter"].get(home)
    away_starter = away_starter or ratings["lastStarter"].get(away)
    def qb_delta(team, starter):
        if not starter or starter not in ratings["qb"]:
            return 0.0
        return ratings["qb"][starter] - ratings["teamPass"].get(team, 0.0)
    return {
        "off": ratings["offense"].get(home, 0.0) - ratings["offense"].get(away, 0.0),
        "def": ratings["defense"].get(away, 0.0) - ratings["defense"].get(home, 0.0),
        "qb": qb_delta(home, home_starter) - qb_delta(away, away_starter),
        "homeStarter": ratings["qbName"].get(home_starter), "awayStarter": ratings["qbName"].get(away_starter),
    }


# ----------------------------------------------------------------------------- fitting

def ols(rows, targets):
    """Ordinary least squares with an intercept, no numpy. rows: list of feature lists."""
    n = len(rows[0]) + 1
    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    for row, y in zip(rows, targets):
        x = [1.0] + list(row)
        for i in range(n):
            xty[i] += x[i] * y
            for j in range(n):
                xtx[i][j] += x[i] * x[j]
    # Gaussian elimination with partial pivoting.
    a = [xtx[i] + [xty[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col] / a[col][col]
            for c in range(col, n + 1):
                a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] if abs(a[i][i]) > 1e-12 else 0.0 for i in range(n)]


def predict_margin(coefficients, features):
    b0, b_off, b_def, b_qb = coefficients
    return b0 + b_off * features["off"] + b_def * features["def"] + b_qb * features["qb"]


# ----------------------------------------------------------------------------- walk-forward

def walk_forward_rows(records, market, seasons, params=None):
    """For every game in `seasons`, features from strictly earlier weeks, the market line, and the result."""
    rows = []
    cache = {}
    for game in sorted(market, key=lambda g: (g["season"], g["week"], g["game_id"])):
        if game["season"] not in seasons:
            continue
        key = (game["season"], game["week"])
        if key not in cache:
            cache[key] = compute_ratings(records, game["season"], game["week"], params)
        feats = game_features(cache[key], game["home"], game["away"])
        rows.append({**game, "features": feats, "actual": game["home_score"] - game["away_score"]})
    return rows


def fit(rows):
    """Fit the margin model, then the blend with the market, on the given rows."""
    x = [[r["features"]["off"], r["features"]["def"], r["features"]["qb"]] for r in rows]
    y = [r["actual"] for r in rows]
    margin_coefficients = ols(x, y)
    blend_x = [[r["spread"], predict_margin(margin_coefficients, r["features"])] for r in rows]
    blend_coefficients = ols(blend_x, y)   # intercept, market weight, model weight
    return {"margin": margin_coefficients, "blend": blend_coefficients}


def evaluate(rows, coefficients, thresholds=(0.0, 1.0, 2.0, 3.0, 4.0)):
    """Winner accuracy and Brier for market, model, and blend; against-the-spread hit rate by disagreement."""
    def summary(margins):
        graded = [(m, r) for m, r in zip(margins, rows) if r["actual"] != 0]
        correct = sum(1 for m, r in graded if (m > 0) == (r["actual"] > 0))
        brier = sum((normal_cdf(m / MARGIN_SD) - (1 if r["actual"] > 0 else 0)) ** 2 for m, r in graded) / len(graded)
        mae = sum(abs(m - r["actual"]) for m, r in graded) / len(graded)
        return {"games": len(graded), "accuracy": round(correct / len(graded), 4), "brier": round(brier, 4), "marginMae": round(mae, 2)}
    market_m = [r["spread"] for r in rows]
    model_m = [predict_margin(coefficients["margin"], r["features"]) for r in rows]
    c0, c1, c2 = coefficients["blend"]
    blend_m = [c0 + c1 * s + c2 * m for s, m in zip(market_m, model_m)]
    ats = []
    for threshold in thresholds:
        picks = [(b, r) for b, r in zip(blend_m, rows) if abs(b - r["spread"]) >= threshold]
        decided = [(b, r) for b, r in picks if r["actual"] != r["spread"]]
        covers = sum(1 for b, r in decided if (b > r["spread"]) == (r["actual"] > r["spread"]))
        ats.append({
            "threshold": threshold, "picks": len(decided), "pushes": len(picks) - len(decided),
            "coverRate": round(covers / len(decided), 4) if decided else None,
        })
    return {
        "market": summary(market_m), "model": summary(model_m), "blend": summary(blend_m),
        "againstTheSpread": ats,
        "blendCoefficients": {"intercept": round(c0, 3), "market": round(c1, 3), "model": round(c2, 3)},
        "marginCoefficients": [round(v, 3) for v in coefficients["margin"]],
    }


def run_walkforward(train_seasons, test_season, params=None):
    all_seasons = sorted(set(train_seasons) | {test_season})
    records = []
    for season in range(min(all_seasons) - 1, max(all_seasons) + 1):
        records.extend(load_team_games(season))
    market = load_market_games(set(all_seasons))
    train_rows = walk_forward_rows(records, market, set(train_seasons), params)
    test_rows = walk_forward_rows(records, market, {test_season}, params)
    coefficients = fit(train_rows)
    report = {
        "trainSeasons": sorted(train_seasons), "testSeason": test_season,
        "trainGames": len(train_rows), "testGames": len(test_rows),
        "params": {**DEFAULTS, **(params or {})},
        "train": evaluate(train_rows, coefficients),
        "test": evaluate(test_rows, coefficients),
        "weekly": [],
    }
    for week in sorted({r["week"] for r in test_rows}):
        week_rows = [r for r in test_rows if r["week"] == week]
        report["weekly"].append({"week": week, **{k: v for k, v in evaluate(week_rows, coefficients).items() if k in ("market", "model", "blend")}})
    return report, coefficients


def current_ratings(season, params=None):
    """Ratings for the live season: everything before the current week, previous seasons included."""
    records = []
    for past in range(season - 4, season + 1):
        records.extend(load_team_games(past))
    played = [r for r in records if r["season"] == season]
    next_week = (max(r["week"] for r in played) + 1) if played else 1
    ratings = compute_ratings(records, season, next_week, params)
    ratings["gamesInSeason"] = len(played) // 2
    return ratings


# ----------------------------------------------------------------------------- live use

def rating_view(ratings, report, home_espn, away_espn, market_home_margin, starter_overrides=None, flag_points=2.0):
    """The rating model's opinion of one live game, for display and for the ledger. Never applied to the live probability."""
    if not ratings or not report or market_home_margin is None:
        return None
    home = ESPN_TO_NFLVERSE.get(home_espn, home_espn)
    away = ESPN_TO_NFLVERSE.get(away_espn, away_espn)
    overrides = starter_overrides or {}
    def starter_id(team_espn, team):
        wanted = overrides.get(team_espn)
        if wanted:
            for pid, name in ratings["qbName"].items():
                if name.lower() == wanted.lower():
                    return pid
        return ratings["lastStarter"].get(team)
    feats = game_features(ratings, home, away, starter_id(home_espn, home), starter_id(away_espn, away))
    coefficients = report["test"]["marginCoefficients"]
    model_margin = predict_margin(coefficients, feats)
    blend = report["test"]["blendCoefficients"]
    blend_margin = blend["intercept"] + blend["market"] * market_home_margin + blend["model"] * model_margin
    disagreement = blend_margin - market_home_margin
    return {
        "modelHomeMargin": round(model_margin, 2),
        "blendHomeMargin": round(blend_margin, 2),
        "ratingHomeWinProbability": round(normal_cdf(blend_margin / MARGIN_SD), 4),
        "disagreementPoints": round(disagreement, 2),
        "flagged": abs(disagreement) >= flag_points,
        "leans": home_espn if disagreement > 0 else away_espn,
        "homeStarter": feats["homeStarter"], "awayStarter": feats["awayStarter"],
        "features": {k: round(v, 4) for k, v in feats.items() if isinstance(v, float)},
        "asOf": ratings.get("cutoff"),
        "blindTest": {
            "season": report["testSeason"],
            "marketAccuracy": report["test"]["market"]["accuracy"], "blendAccuracy": report["test"]["blend"]["accuracy"],
            "marketBrier": report["test"]["market"]["brier"], "blendBrier": report["test"]["blend"]["brier"],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--walkforward", type=int, help="Test season, trained on the seasons before it")
    parser.add_argument("--train-from", type=int, default=2022)
    parser.add_argument("--current", type=int, help="Write live ratings for this season")
    args = parser.parse_args()
    if args.walkforward:
        train = list(range(args.train_from, args.walkforward))
        report, coefficients = run_walkforward(train, args.walkforward)
        out = ROOT / "data" / f"ratings-walkforward-{args.walkforward}.json"
        with out.open("w") as handle:
            json.dump(report, handle, indent=2)
        t = report["test"]
        print(f"Walk-forward {args.walkforward} on {report['testGames']} games (trained {train[0]}-{train[-1]}, {report['trainGames']} games)")
        print(f"  winner accuracy  market {t['market']['accuracy']:.1%}  model {t['model']['accuracy']:.1%}  blend {t['blend']['accuracy']:.1%}")
        print(f"  brier            market {t['market']['brier']:.4f}  model {t['model']['brier']:.4f}  blend {t['blend']['brier']:.4f}")
        print(f"  margin MAE       market {t['market']['marginMae']}  model {t['model']['marginMae']}  blend {t['blend']['marginMae']}")
        for row in t["againstTheSpread"]:
            print(f"  ATS |blend-line|>={row['threshold']}: {row['picks']} picks, cover {row['coverRate']}")
        print(f"  blend = {t['blendCoefficients']}   margin coefficients = {t['marginCoefficients']}")
    if args.current:
        ratings = current_ratings(args.current)
        out = ROOT / "data" / "ratings-current.json"
        with out.open("w") as handle:
            json.dump(ratings, handle, indent=2, sort_keys=True)
        print(f"Wrote ratings as of {ratings['cutoff']} for {len(ratings['offense'])} teams to {out.name}")


if __name__ == "__main__":
    main()
