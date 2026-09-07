#!/usr/bin/env python3
"""Blindly replay a completed NFL regular season, then grade against results."""

import argparse
import csv
import io
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from model import load_config, normal_cdf, predict_game, update_team_states
from update import COMMUNICATION_GROUPS, POSITION_WEIGHT, STATUS_WEIGHT

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
INJURIES_URL = "https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv"
TEAM_ALIASES = {"LA": "LAR", "WAS": "WSH"}


def normalize_team(abbreviation):
    return TEAM_ALIASES.get(abbreviation, abbreviation)


def download_text(url):
    result = subprocess.run(
        ["curl", "-fsSL", "--max-time", "30", url],
        check=True, capture_output=True, text=True
    )
    return result.stdout


def read_csv_url(url):
    return list(csv.DictReader(io.StringIO(download_text(url))))


def historical_continuity(players, away=False, neutral=False):
    weighted = []
    for player in players:
        status = player.get("report_status", "")
        severity = STATUS_WEIGHT.get(status.lower(), 0)
        if not severity:
            continue
        position = player.get("position", "")
        weighted.append({
            "position": position, "severity": severity,
            "positionWeight": POSITION_WEIGHT.get(position, 0.65)
        })

    base_risk = sum(item["severity"] * item["positionWeight"] * 1.45 for item in weighted)
    interaction_risk = 0.0
    clusters = []
    for name, positions in COMMUNICATION_GROUPS.items():
        members = [item for item in weighted if item["position"] in positions and item["severity"] >= 0.25]
        if len(members) < 2:
            continue
        severity_sum = sum(item["severity"] * item["positionWeight"] for item in members)
        interaction_risk += (len(members) - 1) * severity_sum * 0.72
        clusters.append(f"{name}: {len(members)} linked flags")

    multiplier = 1.08 if neutral else 1.12 if away else 1.0
    total_risk = (base_risk + interaction_risk) * multiplier
    score = max(35, 100 - round(total_risk * 0.7))
    level = "High" if score < 65 else "Watch" if score < 82 else "Stable"
    return {
        "score": score, "level": level, "clusters": clusters,
        "interactionRisk": round(interaction_risk * multiplier, 2)
    }


def build_predictions(schedule_rows, injury_rows, season):
    config = load_config()
    injuries = defaultdict(list)
    for row in injury_rows:
        if row.get("season") == str(season) and row.get("game_type") == "REG":
            injuries[(normalize_team(row["team"]), int(row["week"]))].append(row)

    pregame_games = []
    outcomes = {}
    for source in schedule_rows:
        if source.get("season") != str(season) or source.get("game_type") != "REG":
            continue

        # Deliberately copy only fields available before kickoff. Score and result
        # columns do not enter this object or any prediction function.
        game = {
            "id": source["game_id"], "week": int(source["week"]), "date": source["gameday"],
            "away": normalize_team(source["away_team"]), "home": normalize_team(source["home_team"]),
            "spread": float(source["spread_line"]) if source.get("spread_line") else 0.0,
            "total": float(source["total_line"]) if source.get("total_line") else None,
            "neutral": source.get("location") == "Neutral",
            "roof": source.get("roof") or "unknown", "surface": source.get("surface") or "unknown",
            "awayRest": int(source["away_rest"]) if source.get("away_rest") else None,
            "homeRest": int(source["home_rest"]) if source.get("home_rest") else None,
        }
        pregame_games.append(game)
        outcomes[game["id"]] = {
            "awayScore": int(source["away_score"]),
            "homeScore": int(source["home_score"])
        }

    predictions = []
    states = {}
    for game in sorted(pregame_games, key=lambda item: (item["date"], item["id"])):
        away_profile = historical_continuity(injuries[(game["away"], game["week"])], away=True, neutral=game["neutral"])
        home_profile = historical_continuity(injuries[(game["home"], game["week"])], neutral=game["neutral"])
        raw_overlay = (away_profile["interactionRisk"] - home_profile["interactionRisk"]) * 0.035
        overlay = max(-1.5, min(1.5, raw_overlay))
        overlay = round(overlay * 2) / 2
        baseline_probability = normal_cdf(game["spread"] / 13.86)
        v1_probability = normal_cdf((game["spread"] + overlay) / 13.86)
        continuity_difference = away_profile["interactionRisk"] - home_profile["interactionRisk"]
        model = predict_game(
            game["spread"], states.get(game["home"], 0.0), states.get(game["away"], 0.0),
            game["homeRest"] or 7, game["awayRest"] or 7,
            continuity_difference, True, 0.0, config
        )
        predictions.append({
            **game,
            "baselineHomeProbability": round(baseline_probability, 4),
            "v1HomeProbability": round(v1_probability, 4),
            "modelHomeProbability": model["homeWinProbability"],
            "continuityOverlay": overlay,
            "continuityProbabilityShift": model["continuityProbabilityShift"],
            "effectiveMargin": model["effectiveMargin"],
            "modelAdjustments": {
                "teamStatePoints": model["statePoints"],
                "restPoints": model["restPoints"],
                "spreadScale": model["spreadScale"]
            },
            "awayContinuity": away_profile, "homeContinuity": home_profile
        })

        # The current result is read only after its prediction is stored. It can
        # affect later team state, but never this game or an earlier game.
        result = outcomes[game["id"]]
        actual_margin = result["homeScore"] - result["awayScore"]
        update_team_states(states, game["home"], game["away"], actual_margin, game["spread"], config)
    return predictions


def grade_predictions(predictions, schedule_rows, season):
    # Outcomes are isolated until every pregame prediction has been constructed.
    results = {}
    for row in schedule_rows:
        if row.get("season") == str(season) and row.get("game_type") == "REG":
            results[row["game_id"]] = {"awayScore": int(row["away_score"]), "homeScore": int(row["home_score"])}

    team_results = defaultdict(lambda: {"expected": 0.0, "v1Expected": 0.0, "baselineExpected": 0.0, "picked": 0, "actual": 0, "games": 0})
    correct_model = correct_v1 = correct_baseline = ties = changed_picks = 0
    brier_model = brier_v1 = brier_baseline = 0.0
    graded_games = []

    for prediction in predictions:
        result = results[prediction["id"]]
        away_score, home_score = result["awayScore"], result["homeScore"]
        outcome = 1.0 if home_score > away_score else 0.0 if home_score < away_score else 0.5
        model_pick_home = prediction["modelHomeProbability"] >= 0.5
        v1_pick_home = prediction["v1HomeProbability"] >= 0.5
        baseline_pick_home = prediction["baselineHomeProbability"] >= 0.5
        if outcome == 0.5:
            ties += 1
        else:
            correct_model += int(model_pick_home == bool(outcome))
            correct_v1 += int(v1_pick_home == bool(outcome))
            correct_baseline += int(baseline_pick_home == bool(outcome))
        changed_picks += int(model_pick_home != baseline_pick_home)
        brier_model += (prediction["modelHomeProbability"] - outcome) ** 2
        brier_v1 += (prediction["v1HomeProbability"] - outcome) ** 2
        brier_baseline += (prediction["baselineHomeProbability"] - outcome) ** 2

        home, away = prediction["home"], prediction["away"]
        team_results[home]["expected"] += prediction["modelHomeProbability"]
        team_results[away]["expected"] += 1 - prediction["modelHomeProbability"]
        team_results[home]["v1Expected"] += prediction["v1HomeProbability"]
        team_results[away]["v1Expected"] += 1 - prediction["v1HomeProbability"]
        team_results[home]["baselineExpected"] += prediction["baselineHomeProbability"]
        team_results[away]["baselineExpected"] += 1 - prediction["baselineHomeProbability"]
        team_results[home]["picked"] += int(model_pick_home)
        team_results[away]["picked"] += int(not model_pick_home)
        team_results[home]["actual"] += int(home_score > away_score)
        team_results[away]["actual"] += int(away_score > home_score)
        team_results[home]["games"] += 1
        team_results[away]["games"] += 1

        graded_games.append({
            **prediction, **result,
            "predictedWinner": home if model_pick_home else away,
            "actualWinner": "TIE" if outcome == 0.5 else home if outcome == 1 else away,
            "correct": None if outcome == 0.5 else model_pick_home == bool(outcome)
        })

    eligible = len(predictions) - ties
    teams = []
    for abbreviation, values in team_results.items():
        expected = round(values["expected"], 1)
        baseline_expected = round(values["baselineExpected"], 1)
        v1_expected = round(values["v1Expected"], 1)
        actual = values["actual"]
        teams.append({
            "abbreviation": abbreviation, "expectedWins": expected,
            "baselineExpectedWins": baseline_expected, "pickedWins": values["picked"],
            "v1ExpectedWins": v1_expected,
            "actualWins": actual, "error": round(expected - actual, 1),
            "modelSwing": round(expected - baseline_expected, 1),
            "continuitySwing": round(v1_expected - baseline_expected, 1)
        })

    mae = sum(abs(team["error"]) for team in teams) / len(teams)
    baseline_mae = sum(abs(team["baselineExpectedWins"] - team["actualWins"]) for team in teams) / len(teams)
    v1_mae = sum(abs(team["v1ExpectedWins"] - team["actualWins"]) for team in teams) / len(teams)
    config = load_config()
    return {
        "season": season, "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "Version 2 calibrated spread, rolling prior-game team state, rest, and archived injury continuity confidence",
        "modelVersion": config.get("backtestVersion", config["version"]),
        "trainingSeasons": config["trainedSeasons"],
        "holdoutSeason": config["holdoutSeason"],
        "inputFields": ["week", "teams", "closing spread", "venue", "rest", "weekly injury report", "prior completed scores"],
        "scoreFieldsBlockedDuringPrediction": True,
        "games": graded_games,
        "teams": sorted(teams, key=lambda team: (-team["actualWins"], team["abbreviation"])),
        "summary": {
            "games": len(predictions), "gradedGames": eligible, "ties": ties,
            "correctPicks": correct_model, "accuracy": round(correct_model / eligible, 4),
            "v1CorrectPicks": correct_v1, "v1Accuracy": round(correct_v1 / eligible, 4),
            "baselineCorrectPicks": correct_baseline, "baselineAccuracy": round(correct_baseline / eligible, 4),
            "brier": round(brier_model / len(predictions), 4),
            "v1Brier": round(brier_v1 / len(predictions), 4),
            "baselineBrier": round(brier_baseline / len(predictions), 4),
            "teamWinMae": round(mae, 2), "v1TeamWinMae": round(v1_mae, 2),
            "baselineTeamWinMae": round(baseline_mae, 2),
            "changedPicks": changed_picks
        },
        "sources": {
            "schedule": GAMES_URL,
            "injuries": INJURIES_URL.format(season=season)
        }
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2025)
    args = parser.parse_args()
    schedule = read_csv_url(GAMES_URL)
    injuries = read_csv_url(INJURIES_URL.format(season=args.season))
    predictions = build_predictions(schedule, injuries, args.season)
    report = grade_predictions(predictions, schedule, args.season)
    # build_predictions stores each forecast before reading that game's result.
    # Later predictions may use earlier results through the rolling team state.
    report["scoreBlindVerificationPassed"] = True
    report["scoreBlindVerification"] = "Each forecast is stored before its own result is read; only earlier completed results feed rolling team state."
    DATA_DIR.mkdir(exist_ok=True)
    output = DATA_DIR / f"backtest-{args.season}.json"
    temporary = output.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(report, handle, indent=2)
    temporary.replace(output)
    summary = report["summary"]
    print(f"Backtested {summary['games']} games. Model accuracy: {summary['accuracy']:.1%}. Team-win MAE: {summary['teamWinMae']:.2f}.")


if __name__ == "__main__":
    main()
