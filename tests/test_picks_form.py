import json

from picks_form import merge_submissions

PEOPLE = {"narcisa": "Narcisa", "sam": "Sam"}


def sub(created, who="narcisa", **data):
    return {"created_at": created, "data": {"who": who, **data}}


def batch(created, week, games, who="narcisa"):
    return sub(created, who=who, week=str(week), picks=json.dumps(games))


def test_latest_pre_kickoff_submission_wins_and_late_ones_are_ignored():
    kick = {(1, "NE", "SEA"): "2026-09-10T00:20Z"}
    picks = {"sources": {"narcisa": {"label": "Narcisa", "picks": []}}}
    subs = [
        sub("2026-09-08T12:00:00Z", week="1", away="NE", home="SEA", winner="NE", spread=""),
        sub("2026-09-09T12:00:00Z", week="1", away="NE", home="SEA", winner="SEA", spread="NE"),   # changed her mind, still before kickoff
        sub("2026-09-10T03:00:00Z", week="1", away="NE", home="SEA", winner="NE", spread=""),      # after kickoff: ignored
        sub("2026-09-09T12:00:00Z", who="stranger", week="1", away="NE", home="SEA", winner="NE"),
    ]
    picks, kept = merge_submissions(picks, subs, kick, PEOPLE)
    assert kept == 1
    mine = picks["sources"]["narcisa"]["picks"]
    assert len(mine) == 1 and mine[0]["winner"] == "SEA" and mine[0]["spread"] == "NE"
    assert "stranger" not in picks["sources"]


def test_a_weekly_batch_carries_many_games_and_a_later_batch_can_clear_one():
    kick = {(1, "NE", "SEA"): "2026-09-10T00:20Z", (1, "ATL", "PIT"): "2026-09-13T17:00Z", (1, "DEN", "KC"): "2026-09-15T00:15Z"}
    picks = {"sources": {}}
    subs = [
        batch("2026-09-08T12:00:00Z", 1, [
            {"away": "NE", "home": "SEA", "winner": "SEA", "spread": "NE"},
            {"away": "ATL", "home": "PIT", "winner": "PIT", "spread": ""},
            {"away": "DEN", "home": "KC", "winner": "KC", "spread": "KC"},
        ]),
        batch("2026-09-12T12:00:00Z", 1, [
            {"away": "ATL", "home": "PIT", "winner": "", "spread": ""},        # cleared before kickoff
            {"away": "DEN", "home": "KC", "winner": "DEN", "spread": "DEN"},   # changed
        ]),
        batch("2026-09-14T12:00:00Z", 1, [
            {"away": "NE", "home": "SEA", "winner": "NE", "spread": "NE"},     # after that kickoff: ignored
        ]),
    ]
    picks, kept = merge_submissions(picks, subs, kick, PEOPLE)
    mine = {(p["away"], p["home"]): p for p in picks["sources"]["narcisa"]["picks"]}
    assert kept == 2 and set(mine) == {("NE", "SEA"), ("DEN", "KC")}
    assert mine[("NE", "SEA")]["winner"] == "SEA"
    assert mine[("DEN", "KC")]["winner"] == "DEN" and mine[("DEN", "KC")]["spread"] == "DEN"


def test_each_person_gets_their_own_column():
    kick = {(1, "NE", "SEA"): "2026-09-10T00:20Z"}
    picks = {"sources": {}}
    subs = [
        batch("2026-09-08T12:00:00Z", 1, [{"away": "NE", "home": "SEA", "winner": "SEA"}], who="narcisa"),
        batch("2026-09-08T13:00:00Z", 1, [{"away": "NE", "home": "SEA", "winner": "NE"}], who="sam"),
    ]
    picks, kept = merge_submissions(picks, subs, kick, PEOPLE)
    assert kept == 2
    assert picks["sources"]["narcisa"]["picks"][0]["winner"] == "SEA"
    assert picks["sources"]["sam"]["label"] == "Sam" and picks["sources"]["sam"]["picks"][0]["winner"] == "NE"
