import json
from pathlib import Path

import pytest

from scorecard import grade_predictions, record_predictions, summarize


def game(game_id, kickoff, p, m, week=1, completed=False, home_score=None, away_score=None):
    return {
        "id": game_id, "week": week, "kickoff": kickoff, "completed": completed,
        "home": {"abbreviation": "SEA", "score": home_score},
        "away": {"abbreviation": "NE", "score": away_score},
        "homeWinProbability": p, "marketHomeWinProbability": m, "marketHomeMargin": 3.5,
        "probabilitySource": "test",
    }


def test_forecast_refreshes_before_kickoff_and_freezes_at_kickoff():
    ledger = {"season": 2026, "games": {}}
    record_predictions(ledger, [game("1", "2026-09-10T00:20Z", 0.60, 0.61)], "2026-09-08T12:00Z")
    record_predictions(ledger, [game("1", "2026-09-10T00:20Z", 0.64, 0.61)], "2026-09-09T12:00Z")
    assert ledger["games"]["1"]["homeWinProbability"] == 0.64
    assert ledger["games"]["1"]["frozen"] is False

    record_predictions(ledger, [game("1", "2026-09-10T00:20Z", 0.90, 0.61)], "2026-09-10T03:00Z")
    entry = ledger["games"]["1"]
    assert entry["frozen"] is True
    assert entry["homeWinProbability"] == 0.64, "a forecast recorded after kickoff must not replace the frozen one"


def test_game_first_seen_after_kickoff_is_late_and_never_graded():
    ledger = {"season": 2026, "games": {}}
    record_predictions(ledger, [game("2", "2026-09-10T00:20Z", 0.99, 0.61)], "2026-09-11T12:00Z")
    assert ledger["games"]["2"]["late"] is True
    grade_predictions(ledger, [game("2", "2026-09-10T00:20Z", 0.99, 0.61, completed=True, home_score=30, away_score=10)])
    assert "graded" not in ledger["games"]["2"]
    assert summarize(ledger)["overall"]["graded"] == 0
    assert summarize(ledger)["late"] == 1


def test_grading_scores_model_and_market_side_by_side():
    ledger = {"season": 2026, "games": {}}
    record_predictions(ledger, [game("3", "2026-09-10T00:20Z", 0.70, 0.40)], "2026-09-09T12:00Z")
    record_predictions(ledger, [game("3", "2026-09-10T00:20Z", 0.70, 0.40)], "2026-09-10T01:00Z")
    grade_predictions(ledger, [game("3", "2026-09-10T00:20Z", 0.70, 0.40, completed=True, home_score=24, away_score=20)])
    entry = ledger["games"]["3"]
    assert entry["modelCorrect"] is True and entry["marketCorrect"] is False
    assert entry["modelBrier"] == pytest.approx(0.09)
    assert entry["marketBrier"] == pytest.approx(0.36)
    summary = summarize(ledger)["overall"]
    assert summary["modelAccuracy"] == 1.0 and summary["marketAccuracy"] == 0.0
    assert summary["brierEdge"] == pytest.approx(0.27)
    assert summary["picksDiffered"] == 1 and summary["modelWonDisagreements"] == 1


def test_ties_are_excluded_from_accuracy():
    ledger = {"season": 2026, "games": {}}
    record_predictions(ledger, [game("4", "2026-09-10T00:20Z", 0.55, 0.55)], "2026-09-09T12:00Z")
    record_predictions(ledger, [game("4", "2026-09-10T00:20Z", 0.55, 0.55)], "2026-09-10T01:00Z")
    grade_predictions(ledger, [game("4", "2026-09-10T00:20Z", 0.55, 0.55, completed=True, home_score=20, away_score=20)])
    summary = summarize(ledger)["overall"]
    assert summary["graded"] == 0


SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "snapshot.json"


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="no live snapshot")
def test_live_snapshot_board_follows_the_market():
    # Invariant that would have caught the inverted board: the live forecast may differ from
    # the market-implied wins by a little, never by a lot, and the totals must sum to 272.
    snapshot = json.loads(SNAPSHOT.read_text())
    teams = snapshot["teams"]
    assert sum(team["projectedWins"] for team in teams) == pytest.approx(272, abs=1.0)
    for team in teams:
        if team.get("marketProjectedWins") is None:
            continue
        assert abs(team["projectedWins"] - team["marketProjectedWins"]) < 2.5, team["name"]
    for game in snapshot["games"]:
        if game.get("lineSource") == "Market line" and not game["completed"]:
            favored_home = game["marketHomeMargin"] > 0
            # the spread-derived probability must agree with the spread's sign; the moneyline-derived one
            # may sit at exactly one half when the book prices both sides the same on a 1.5-point line
            assert (game["marketSpreadHomeWinProbability"] > 0.5) == favored_home or game["marketHomeMargin"] == 0


def test_closing_line_value_measures_movement_toward_the_early_pick():
    from scorecard import closing_line_value
    ledger = {"season": 2026, "games": {}}
    # Tuesday: market has home -1 (55%), model leans away (45%). By kickoff the line moved to away -2.
    early = game("5", "2026-09-13T17:00Z", 0.45, 0.55)
    early["marketHomeMargin"] = 1.0
    record_predictions(ledger, [early], "2026-09-08T12:00Z")
    late = game("5", "2026-09-13T17:00Z", 0.42, 0.44)
    late["marketHomeMargin"] = -2.0
    record_predictions(ledger, [late], "2026-09-12T12:00Z")
    record_predictions(ledger, [late], "2026-09-13T18:00Z")   # kickoff passed: freeze, record the close
    entry = ledger["games"]["5"]
    assert entry["frozen"] and entry["closingHomeMargin"] == -2.0
    assert entry["first"]["marketHomeMargin"] == 1.0
    assert len(entry["history"]) == 2
    assert closing_line_value(entry) == 3.0        # line moved 3 points toward the away side the model liked
    clv = summarize(ledger)["closingLineValue"]
    assert clv["games"] == 1 and clv["disagreements"] == 1
    assert clv["disagreementMeanPoints"] == 3.0 and clv["positiveShare"] == 1.0


def test_closing_line_value_is_negative_when_the_market_moves_against_the_pick():
    from scorecard import closing_line_value
    entry = {"first": {"marketHomeMargin": 3.0, "homeWinProbability": 0.62, "marketHomeWinProbability": 0.59},
             "closingHomeMargin": 1.0, "frozen": True}
    assert closing_line_value(entry) == -2.0


def test_rating_second_opinion_is_recorded_and_graded_against_the_close():
    ledger = {"season": 2026, "games": {}}
    g = game("6", "2026-09-13T17:00Z", 0.60, 0.61)
    g["marketHomeMargin"] = 3.0
    g["ratingModel"] = {"ratingHomeWinProbability": 0.66, "blendHomeMargin": 6.0, "flagged": True}
    record_predictions(ledger, [g], "2026-09-12T12:00Z")
    record_predictions(ledger, [g], "2026-09-13T18:00Z")
    done = game("6", "2026-09-13T17:00Z", 0.60, 0.61, completed=True, home_score=27, away_score=20)
    grade_predictions(ledger, [done])
    entry = ledger["games"]["6"]
    assert entry["ratingCorrect"] is True
    assert entry["flaggedCovered"] is True     # home won by 7, blend said home by 6 vs a close of 3
    summary = summarize(ledger)["overall"]
    assert summary["ratingGraded"] == 1 and summary["flaggedPicks"] == 1 and summary["flaggedCovered"] == 1


def test_outside_picks_are_frozen_and_graded_straight_up_and_against_the_spread():
    ledger = {"season": 2026, "games": {}}
    g = game("7", "2026-09-13T17:00Z", 0.60, 0.61)
    g["marketHomeMargin"] = 3.0
    g["outsidePicks"] = {"cbs": {"label": "CBS", "winner": "SEA", "spread": "NE"}}
    record_predictions(ledger, [g], "2026-09-12T12:00Z")
    record_predictions(ledger, [g], "2026-09-13T18:00Z")
    done = game("7", "2026-09-13T17:00Z", 0.60, 0.61, completed=True, home_score=21, away_score=20)
    grade_predictions(ledger, [done])
    pick = ledger["games"]["7"]["outsidePicks"]["cbs"]
    assert pick["winnerCorrect"] is True        # SEA won
    assert pick["spreadCovered"] is True        # NE +3 covered a 1-point loss
    src = summarize(ledger)["overall"]["outsideSources"]["cbs"]
    assert src["winnerAccuracy"] == 1.0 and src["spreadCoverRate"] == 1.0 and src["spreadPicks"] == 1
