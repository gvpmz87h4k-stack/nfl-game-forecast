from datetime import datetime, timezone

from update import continuity_profile

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
GAME = {"venue": {"neutral": False}, "weather": {"summary": "Clear"}}


def team(*players):
    return {"injuries": [dict(zip(("player", "position", "status", "updated"), p)) for p in players]}


def test_starters_count_fully_and_backups_and_unlisted_count_less():
    lineup = {"starters": {"A Starter", "B Starter"}, "all": {"A Starter", "B Starter", "C Backup"}}
    full = continuity_profile(team(("A Starter", "OT", "Out", "2026-09-08T00:00Z"), ("B Starter", "C", "Out", "2026-09-08T00:00Z")), GAME, "home", lineup, NOW)
    bench = continuity_profile(team(("C Backup", "OT", "Out", "2026-09-08T00:00Z"), ("D Nobody", "C", "Out", "2026-09-08T00:00Z")), GAME, "home", lineup, NOW)
    assert full["score"] < bench["score"]
    assert full["clusters"] == ["Protection unit: 2 linked flags"]
    assert bench["clusters"] == []            # two discounted names do not make a cluster
    assert {d["role"] for d in bench["discounted"]} == {"backup", "unlisted"}
    assert full["clusterMembers"]["Protection unit"][0]["role"] == "starter"


def test_old_injured_reserve_is_nearly_free_and_fresh_injured_reserve_is_not():
    lineup = {"starters": {"A Starter", "B Starter"}, "all": {"A Starter", "B Starter"}}
    fresh = continuity_profile(team(("A Starter", "OT", "Injured Reserve", "2026-09-07T00:00Z"), ("B Starter", "C", "Injured Reserve", "2026-09-07T00:00Z")), GAME, "home", lineup, NOW)
    old = continuity_profile(team(("A Starter", "OT", "Injured Reserve", "2026-08-01T00:00Z"), ("B Starter", "C", "Injured Reserve", "2026-08-01T00:00Z")), GAME, "home", lineup, NOW)
    assert fresh["score"] < old["score"] and old["score"] >= 95
    assert old["discounted"][0]["why"].startswith("long-term")


def test_without_a_depth_chart_everyone_counts_as_before():
    profile = continuity_profile(team(("Anyone", "QB", "Out", "2026-09-08T00:00Z")), GAME, "away", None, NOW)
    assert profile["lineupRead"] is False and profile["discounted"] == [] and profile["score"] < 100


def test_unreadable_or_zoneless_dates_never_crash_the_run():
    lineup = {"starters": {"A Starter"}, "all": {"A Starter"}}
    zoneless = continuity_profile(team(("A Starter", "OT", "Injured Reserve", "2026-08-01")), GAME, "home", lineup, NOW)
    assert zoneless["discounted"][0]["why"].startswith("long-term")
    garbage = continuity_profile(team(("A Starter", "OT", "Injured Reserve", "yesterday")), GAME, "home", lineup, NOW)
    assert garbage["discounted"] == [] and garbage["score"] < 100
