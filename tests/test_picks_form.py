from picks_form import merge_submissions


def sub(created, **data):
    return {"created_at": created, "data": {"who": "narcisa", **data}}


def test_latest_pre_kickoff_submission_wins_and_late_ones_are_ignored():
    kick = {(1, "NE", "SEA"): "2026-09-10T00:20Z"}
    picks = {"sources": {"narcisa": {"label": "Narcisa", "picks": []}}}
    subs = [
        sub("2026-09-08T12:00:00Z", week="1", away="NE", home="SEA", winner="NE", spread=""),
        sub("2026-09-09T12:00:00Z", week="1", away="NE", home="SEA", winner="SEA", spread="NE"),   # changed her mind, still before kickoff
        sub("2026-09-10T03:00:00Z", week="1", away="NE", home="SEA", winner="NE", spread=""),      # after kickoff: ignored
        {"created_at": "2026-09-09T12:00:00Z", "data": {"who": "stranger", "week": "1", "away": "NE", "home": "SEA", "winner": "NE"}},
    ]
    picks, kept = merge_submissions(picks, subs, kick)
    assert kept == 1
    mine = picks["sources"]["narcisa"]["picks"]
    assert len(mine) == 1 and mine[0]["winner"] == "SEA" and mine[0]["spread"] == "NE"
    assert "stranger" not in picks["sources"]
