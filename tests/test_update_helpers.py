from datetime import datetime, timezone

from update import (
    infer_current_week,
    normalize_preparation_history,
    player_key,
    parse_margin,
    parse_odds,
    roster_preparation_events,
    season_table,
    travel_team_score,
    build_travel_profile,
    continuity_profile,
    compute_line_movement_signal,
)


def test_player_key_normalizes_team_and_name():
    assert player_key("SF", "Player-Name Jr.") == "SF|playernamejr"


def test_normalize_preparation_history_removes_official_leave_tag():
    source = {
        "players": {
            "ignored": {
                "team": "SF",
                "player": "Alex",
                "position": "QB",
                "observations": [
                    {"date": "2026-01-02", "categories": ["Official leave", "Missed practice"]},
                    {"date": "2026-01-02", "categories": ["Missed practice"]},
                ],
            },
        },
    }
    normalized = normalize_preparation_history(source)
    key = player_key("SF", "Alex")
    assert key in normalized["players"]
    assert normalized["players"][key]["observations"] == [{"date": "2026-01-02", "categories": ["Missed practice"]}]


def test_roster_preparation_events_filters_to_current_week_and_tracks_late_activation():
    rows = [
        {"season": "2026", "game_type": "REG", "week": "3", "team": "SF", "full_name": "Old Player", "position": "QB", "status": "SUS"},
        {"season": "2026", "game_type": "REG", "week": "2", "team": "SF", "full_name": "Late Return", "position": "WR", "status": "ACT"},
        {"season": "2025", "game_type": "REG", "week": "2", "team": "SF", "full_name": "Wrong Year", "position": "RB", "status": "SUS"},
    ]
    now = datetime(2026, 9, 6, tzinfo=timezone.utc)
    events, statuses = roster_preparation_events(rows, 2026, 2, {player_key("SF", "late return"): "SUS"}, now)

    assert len(events) == 1
    assert events[0]["categories"] == ["Late activation"]
    assert statuses[player_key("SF", "Late Return")] == "ACT"


def test_travel_scoring_and_travel_profile_are_consistent():
    team = {
        "localSleepCycles": "8",
        "plannedLocalPracticeSessions": "7",
        "internationalGames": "4",
        "coachInternationalGames": "3",
        "surfaceFamiliarity": "1",
        "separateOrLateTravelers": "5",
    }
    score, components = travel_team_score(team)

    assert score == 6.6
    assert components["surfaceFamiliarity"] == 1

    profile = build_travel_profile({
        "away": team,
        "home": team,
        "uncertaintyMultiplier": 1.15,
        "shared": {"timeZoneJumpHours": 3},
    })
    assert profile["available"] is True
    assert profile["applied"] is False
    assert profile["away"]["acclimationScore"] == 6.6

    missing_profile = build_travel_profile(None)
    assert missing_profile["available"] is False
    assert missing_profile["applied"] is False


def test_parse_margin_returns_home_margin_positive_when_home_favored():
    # Model convention: positive = home team favored (normal_cdf(margin) is home win probability).
    assert parse_margin("NYG -6", "NYG", "DAL") == 6.0      # home favorite -> positive
    assert parse_margin("NYG -6", "DAL", "NYG") == -6.0     # away favorite -> negative
    assert parse_margin("DAL +6", "NYG", "DAL") == 6.0      # written from the underdog side
    assert parse_margin("PK", "DAL", "NYG") == 0.0
    assert parse_margin("-3.5", "SEA", "NE") == 3.5         # bare ESPN spread, home-relative


def test_parse_odds_keeps_opening_and_current_on_the_same_sign():
    # Regression for the inverted season board: the opening line used to lose its sign,
    # so every 3.5-point favorite read as a 7-point move toward the underdog.
    competition = {"odds": [{"details": "SEA -3.5", "overUnder": 44.5, "spread": -3.5}]}
    odds = parse_odds(competition, "SEA", "NE")
    assert odds["homeMargin"] == 3.5
    assert odds["openingHomeMargin"] == 3.5
    assert odds["lineMovement"] == 0.0


def test_infer_current_week_and_continuity():
    games = [
        {"kickoff": "2026-09-14T20:00:00Z", "week": 1},
        {"kickoff": "2026-09-08T20:00:00Z", "week": 2},
        {"kickoff": "2026-09-01T20:00:00Z", "week": 3},
    ]
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert infer_current_week(games, now) == 2

    injuries = {"injuries": []}
    team = {"injuries": []}
    for _ in injuries.values():
        break
    game = {"venue": {"neutral": True}, "weather": {"summary": "rain"}}
    result = continuity_profile(team, game, "away")
    assert result["clusters"] == []
    assert result["level"] in {"High", "Watch", "Stable"}


def test_line_movement_signal_defaults_to_market_difference():
    game = {
        "odds": {"lineMovement": -1.25},
        "marketHomeMargin": 3.0,
        "venue": {"neutral": False},
        "home": {"abbreviation": "SF", "record": "8-1"},
        "away": {"abbreviation": "LAR", "record": "6-3"},
        "travel": {"home": {"surfaceFamiliarity": 0.6}, "away": {"surfaceFamiliarity": 0.2}}
    }
    travel_overrides = {}
    signal = compute_line_movement_signal(game["odds"], travel_overrides)
    assert signal["difference"] == -1.25
    assert signal["source"] == "market line movement"


def test_parse_odds_reads_espn_opening_line_prices_and_moneyline():
    from update import parse_odds, devig
    competition = {"odds": [{
        "details": "SEA -3.5", "overUnder": 44.5, "spread": -3.5, "provider": {"name": "DraftKings"},
        "pointSpread": {"home": {"open": {"line": "-3.5", "odds": "-110"}, "close": {"line": "-4.5", "odds": "-105"}},
                        "away": {"open": {"line": "+3.5", "odds": "-110"}, "close": {"line": "+4.5", "odds": "-115"}}},
        "moneyline": {"home": {"open": {"odds": "-192"}, "close": {"odds": "-180"}},
                      "away": {"open": {"odds": "+160"}, "close": {"odds": "+150"}}},
    }]}
    odds = parse_odds(competition, "SEA", "NE")
    assert odds["homeMargin"] == 4.5 and odds["openingHomeMargin"] == 3.5
    assert odds["lineMovement"] == 1.0            # moved one point toward the home side
    assert odds["book"] == "DraftKings"
    assert odds["spreadPrice"]["home"] == "-105" and odds["spreadPrice"]["homeOpen"] == "-110"
    assert odds["moneylineHomeProbability"] == 0.6164   # -180 -> .643, +150 -> .400, fee removed: .643/1.043
    assert devig("-110", "-110") == 0.5
    assert devig(None, "+150") is None
