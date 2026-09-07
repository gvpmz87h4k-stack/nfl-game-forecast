from situations import flags


def g(season, week, home, away, spread, hs=None, as_=None, hr=7, ar=7, div=False):
    return {"game_id": f"{season}_{week:02d}_{away}_{home}", "season": season, "week": week, "home": home, "away": away,
            "spread": spread, "home_score": hs, "away_score": as_, "home_rest": hr, "away_rest": ar, "divisional": div}


def test_flags_mark_bye_short_blowout_lookahead_and_divisional():
    games = [
        g(2026, 1, "SEA", "NE", 3.5, 30, 10),                      # NE loses by 20 -> blowout next week
        g(2026, 2, "SEA", "SF", 8.0, hr=14, ar=5, div=True),        # SEA off a bye, 8-point favorite; SF short week
        g(2026, 2, "NE", "DAL", -2.0),                              # NE coming off the blowout
        g(2026, 3, "SEA", "LAR", 1.0),                              # SEA's next game is close -> week 2 was a lookahead spot
    ]
    out = {x["game_id"]: x for x in flags(games)}
    week2 = out["2026_02_SF_SEA"]
    assert week2["homeFlags"]["bye"] == 1 and week2["awayFlags"]["short"] == 1
    assert week2["homeFlags"]["lookahead"] == 1 and week2["divisional"] is True
    assert week2["features"]["bye"] == 1 and week2["features"]["short"] == -1
    assert out["2026_02_DAL_NE"]["homeFlags"]["blowout"] == 1
    assert out["2026_01_NE_SEA"]["homeFlags"]["blowout"] == 0
