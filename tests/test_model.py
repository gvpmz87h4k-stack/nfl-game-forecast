import pytest

from model import clamp, normal_cdf, predict_game, quantile, simulate_season, update_team_states


BASE_CONFIG = {
    "spreadScale": 1.0,
    "strongFavoriteThreshold": 9.0,
    "strongFavoriteScale": 1.25,
    "teamStateWeight": 0.1,
    "restDayWeight": 0.08,
    "marginStandardDeviation": 10.0,
    "continuityProbabilityWeight": 0.001,
    "continuityProbabilityCap": 0.02,
    "preparationProbabilityWeight": 0.001,
    "preparationProbabilityCap": 0.02,
    "lineMovementProbabilityWeight": 0.00125,
    "lineMovementProbabilityCap": 0.01,
    "travelScoreProbabilityWeight": 0.002,
    "travelProbabilityCap": 0.02,
    "teamStateDecay": 0.85,
    "latentTeamSigma": 2.5,
}


def test_clamp_and_normal_cdf_are_reasonable():
    assert clamp(-1, 0.0, 1.0) == 0.0
    assert clamp(2, 0.0, 1.0) == 1.0
    assert clamp(0.25, 0.0, 1.0) == 0.25

    assert normal_cdf(0.0) == 0.5


def test_predict_game_supports_optional_features():
    default_prediction = predict_game(
        6.0,
        home_state=8.0,
        away_state=3.0,
        home_rest=9,
        away_rest=7,
        continuity_difference=30,
        apply_continuity=False,
        preparation_difference=20,
        apply_preparation=False,
        travel_score_difference=20,
        apply_travel=False,
        config=BASE_CONFIG,
    )

    assert default_prediction["homeWinProbability"] == 0.7473
    assert default_prediction["continuityProbabilityShift"] == 0.0
    assert default_prediction["preparationProbabilityShift"] == 0.0
    assert default_prediction["travelProbabilityShift"] == 0.0

    active_prediction = predict_game(
        6.0,
        home_state=8.0,
        away_state=3.0,
        home_rest=9,
        away_rest=7,
        continuity_difference=30,
        apply_continuity=True,
        manual_points=0.0,
        preparation_difference=0.0,
        apply_preparation=False,
        travel_score_difference=0.0,
        uncertainty_multiplier=1.0,
        apply_travel=False,
        config=BASE_CONFIG,
    )

    assert active_prediction["continuityProbabilityShift"] == 0.02
    assert active_prediction["travelProbabilityShift"] == 0.0
    assert active_prediction["homeWinProbability"] > default_prediction["homeWinProbability"]

    strong_favorite = predict_game(
        20.0,
        config=BASE_CONFIG,
    )

    assert strong_favorite["spreadScale"] == 1.25
    assert strong_favorite["homeWinProbability"] == 0.98


def test_line_movement_shift_is_capped_and_cannot_flip_the_favorite():
    output = predict_game(
        6.0, config=BASE_CONFIG, apply_continuity=False, apply_preparation=False, apply_travel=False,
        line_movement_difference=5000, apply_line_movement=True,
    )
    assert output["lineMovementProbabilityShift"] == 0.01
    assert output["homeWinProbability"] > output["marketOnlyProbability"]

    against = predict_game(
        1.0, config=BASE_CONFIG, apply_continuity=False, apply_preparation=False, apply_travel=False,
        line_movement_difference=-5000, apply_line_movement=True,
    )
    assert against["homeWinProbability"] > 0.5, "a capped shift must never reverse the market favorite"


def test_update_team_states_and_quantile_are_stable():
    states = {}
    update_team_states(states, "SF", "SEA", actual_margin=20.0, market_margin=12.0, config=BASE_CONFIG)

    assert states["SF"] == pytest.approx(1.2)
    assert states["SEA"] == pytest.approx(-1.2)

    values = [2, 4, 6, 8, 10]
    assert quantile(values, 0.0) == 2
    assert quantile(values, 1.0) == 10
    assert quantile(values, 0.5) == 6


def test_simulate_season_is_reproducible():
    games = [
        {
            "home": {"abbreviation": "SF", "score": 21},
            "away": {"abbreviation": "SEA", "score": 10},
            "completed": True,
            "effectiveMargin": 0.0,
        },
        {
            "home": {"abbreviation": "DAL", "score": 0},
            "away": {"abbreviation": "GB", "score": 0},
            "completed": False,
            "effectiveMargin": 0.0,
            "continuity": {"probabilityShift": 0.0},
            "preparation": {"probabilityShift": 0.0},
            "travel": {"probabilityShift": 0.0, "uncertaintyMultiplier": 1.0},
        },
    ]

    first = simulate_season(games, ["SF", "SEA", "DAL", "GB"], simulations=200, seed=2026, config=BASE_CONFIG)
    second = simulate_season(games, ["SF", "SEA", "DAL", "GB"], simulations=200, seed=2026, config=BASE_CONFIG)

    assert first == second
    assert first["SF"]["mean"] >= 0.9
    assert first["SF"]["mean"] <= 1.1
    assert first["DAL"]["mean"] + first["GB"]["mean"] == 1.0
