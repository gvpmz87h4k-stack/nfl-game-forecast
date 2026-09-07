"""Shared calibrated prediction functions for live forecasts and backtests."""

import json
import math
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "model-config.json"


def load_config():
    with CONFIG_PATH.open() as handle:
        return json.load(handle)


def normal_cdf(value):
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def clamp(value, low, high):
    return min(high, max(low, value))


def predict_game(market_margin, home_state=0.0, away_state=0.0,
                 home_rest=7, away_rest=7, continuity_difference=0.0,
                 apply_continuity=True, manual_points=0.0, config=None,
                 preparation_difference=0.0, apply_preparation=True,
                 travel_score_difference=0.0, uncertainty_multiplier=1.0,
                 apply_travel=True, line_movement_difference=0.0,
                 apply_line_movement=True):
    """Home win probability from the market line plus a few small, capped confidence shifts.

    market_margin is the expected home margin (positive = home favored). Everything after
    the calibrated line is a confidence dial: none of the shifts can flip the favorite.
    """
    config = config or load_config()
    spread = market_margin * config["spreadScale"]
    strong_multiplier = config["strongFavoriteScale"] if abs(market_margin) >= config["strongFavoriteThreshold"] else 1.0
    spread *= strong_multiplier
    market_only_probability = normal_cdf(spread / config["marginStandardDeviation"])
    state_points = (home_state - away_state) * config["teamStateWeight"]
    rest_difference = clamp(home_rest - away_rest, -7, 7)
    rest_points = rest_difference * config["restDayWeight"]
    effective_margin = spread + state_points + rest_points + manual_points
    standard_probability = normal_cdf(effective_margin / config["marginStandardDeviation"])
    uncertainty_multiplier = max(1.0, uncertainty_multiplier if apply_travel else 1.0)
    base_probability = normal_cdf(
        effective_margin / (config["marginStandardDeviation"] * uncertainty_multiplier)
    )

    def shift(enabled, difference, weight_key, cap_key):
        if not enabled:
            return 0.0
        cap = config.get(cap_key, 0.0)
        return clamp(difference * config.get(weight_key, 0.0), -cap, cap)

    continuity_shift = shift(apply_continuity, continuity_difference, "continuityProbabilityWeight", "continuityProbabilityCap")
    preparation_shift = shift(apply_preparation, preparation_difference, "preparationProbabilityWeight", "preparationProbabilityCap")
    line_movement_shift = shift(apply_line_movement, line_movement_difference, "lineMovementProbabilityWeight", "lineMovementProbabilityCap")
    travel_shift = shift(apply_travel, travel_score_difference, "travelScoreProbabilityWeight", "travelProbabilityCap")

    probability = base_probability
    for value in (continuity_shift, preparation_shift, line_movement_shift, travel_shift):
        probability = clamp(probability + value, 0.02, 0.98)
    if (base_probability - 0.5) * (probability - 0.5) < 0:
        probability = 0.5001 if base_probability > 0.5 else 0.4999

    return {
        "homeWinProbability": round(probability, 4),
        "baseProbability": round(base_probability, 4),
        "marketOnlyProbability": round(market_only_probability, 4),
        "uncertaintyProbabilityShift": round(base_probability - standard_probability, 4),
        "effectiveMargin": round(effective_margin, 3),
        "spreadScale": config["spreadScale"] * strong_multiplier,
        "statePoints": round(state_points, 2),
        "restPoints": round(rest_points, 2),
        "continuityProbabilityShift": round(continuity_shift, 4),
        "preparationProbabilityShift": round(preparation_shift, 4),
        "lineMovementProbabilityShift": round(line_movement_shift, 4),
        "travelProbabilityShift": round(travel_shift, 4),
        "uncertaintyMultiplier": round(uncertainty_multiplier, 3)
    }


def update_team_states(states, home, away, actual_margin, market_margin, config=None):
    config = config or load_config()
    decay = config["teamStateDecay"]
    surprise = actual_margin - market_margin
    states[home] = decay * states.get(home, 0.0) + (1 - decay) * surprise
    states[away] = decay * states.get(away, 0.0) - (1 - decay) * surprise


def quantile(values, probability):
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * probability))
    return ordered[index]


def simulate_season(games, team_abbreviations, simulations=5000, seed=20260906, config=None):
    config = config or load_config()
    rng = random.Random(seed)
    distributions = {team: [] for team in team_abbreviations}
    sigma = config["latentTeamSigma"]
    for _ in range(simulations):
        wins = defaultdict(int)
        latent = {team: rng.gauss(0, sigma) for team in team_abbreviations}
        for game in games:
            home = game["home"]["abbreviation"]
            away = game["away"]["abbreviation"]
            if game["completed"]:
                if game["home"]["score"] > game["away"]["score"]:
                    wins[home] += 1
                elif game["away"]["score"] > game["home"]["score"]:
                    wins[away] += 1
                continue
            probability = normal_cdf(
                (game["effectiveMargin"] + latent[home] - latent[away]) /
                (config["marginStandardDeviation"] * game.get("travel", {}).get("uncertaintyMultiplier", 1.0))
            )
            probability = clamp(probability + game.get("continuity", {}).get("probabilityShift", 0), 0.02, 0.98)
            probability = clamp(probability + game.get("preparation", {}).get("probabilityShift", 0), 0.02, 0.98)
            probability = clamp(probability + game.get("lineMovement", {}).get("probabilityShift", 0), 0.02, 0.98)
            probability = clamp(probability + game.get("travel", {}).get("probabilityShift", 0), 0.02, 0.98)
            if rng.random() < probability:
                wins[home] += 1
            else:
                wins[away] += 1
        for team in team_abbreviations:
            distributions[team].append(wins[team])

    return {
        team: {
            "p10": quantile(values, 0.10), "median": quantile(values, 0.50),
            "p90": quantile(values, 0.90), "mean": round(sum(values) / len(values), 1)
        }
        for team, values in distributions.items()
    }
