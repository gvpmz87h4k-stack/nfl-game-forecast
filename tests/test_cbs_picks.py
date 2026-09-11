from cbs_picks import build_entries, consensus, merge, parse_table

ROW = '''<tr><td class="GameMatchup"><a href="/nfl/gametracker/preview/NFL_20260909_{away}@{home}/">Preview</a></td>{cells}</tr>'''
CELL = '<td class="TableExpertPicks-pickLayout">{text}</td>'
HEAD = '''<thead><tr><th>Matchup</th><th>Pete Prisco Senior Writer</th><th>Jared Dubin Writer</th><th>Ryan Wilson NFL Draft analyst</th></tr></thead>'''


def page(rows):
    return f'<table class="TableBase-table TableExpertPicks hasStickyHeader">{HEAD}<tbody>{"".join(rows)}</tbody></table>'


def row(away, home, texts):
    return ROW.format(away=away, home=home, cells="".join(CELL.format(text=t) for t in texts))


def test_parse_table_reads_experts_games_and_cell_aligned_picks():
    experts, games = parse_table(page([
        row("NE", "SEA", ["SEA -3.5 Remove", "", "NE +3.5 Remove"]),
        row("JAC", "WAS", ["WAS Remove", "JAC Remove", "WAS Remove"]),
    ]))
    assert experts == ["Pete Prisco", "Jared Dubin", "Ryan Wilson"]
    assert games[0]["away"] == "NE" and games[0]["picks"] == ["SEA", None, "NE"]
    assert games[1]["away"] == "JAX" and games[1]["home"] == "WSH"      # CBS codes mapped to ESPN codes
    assert games[1]["picks"] == ["WSH", "JAX", "WSH"]


def test_consensus_is_a_strict_majority():
    assert consensus(["SEA", "SEA", "NE"]) == "SEA"
    assert consensus(["SEA", "NE", None]) is None
    assert consensus([None, None]) is None


def test_build_entries_records_every_expert_and_the_consensus():
    winner = parse_table(page([row("NE", "SEA", ["SEA", "SEA", "NE"])]))
    spread = parse_table(page([row("NE", "SEA", ["NE +3.5", "NE +3.5", "SEA -3.5"])]))
    sources = build_entries(1, {"winner": winner, "spread": spread})
    assert sources["cbs"]["picks"] == [{"week": 1, "away": "NE", "home": "SEA", "winner": "SEA", "spread": "NE"}]
    assert sources["cbs-prisco"]["picks"][0] == {"week": 1, "away": "NE", "home": "SEA", "winner": "SEA", "spread": "NE"}
    assert sources["cbs-wilson"]["picks"][0]["winner"] == "NE" and sources["cbs-wilson"]["picks"][0]["spread"] == "SEA"


def test_merge_replaces_only_this_week_and_keeps_other_sources():
    existing = {"sources": {
        "cbs": {"label": "old", "picks": [{"week": 1, "away": "A", "home": "B", "winner": "A"}, {"week": 2, "away": "C", "home": "D", "winner": "C"}]},
        "friend": {"label": "A friend", "picks": [{"week": 2, "away": "C", "home": "D", "winner": "D"}]},
    }}
    merged = merge(existing, 2, {"cbs": {"label": "CBS Sports experts, consensus", "picks": [{"week": 2, "away": "C", "home": "D", "winner": "D"}]}})
    assert [p["week"] for p in merged["sources"]["cbs"]["picks"]] == [1, 2]
    assert merged["sources"]["cbs"]["picks"][1]["winner"] == "D"
    assert merged["sources"]["friend"]["picks"][0]["winner"] == "D"


def test_merge_leaves_games_that_have_kicked_off_alone():
    existing = {"sources": {"cbs": {"label": "CBS", "picks": [
        {"week": 1, "away": "NE", "home": "SEA", "winner": "NE", "spread": "SEA"},
        {"week": 1, "away": "ATL", "home": "PIT", "winner": "PIT"},
    ]}}}
    fresh = {"cbs": {"label": "CBS Sports experts, consensus", "picks": [
        {"week": 1, "away": "NE", "home": "SEA", "spread": "SEA"},          # CBS trimmed the finished game to spread only
        {"week": 1, "away": "ATL", "home": "PIT", "winner": "ATL"},         # a writer changed his mind before kickoff
    ]}}
    merged = merge(existing, 1, fresh, started={("NE", "SEA")})
    by_game = {(p["away"], p["home"]): p for p in merged["sources"]["cbs"]["picks"]}
    assert by_game[("NE", "SEA")]["winner"] == "NE"        # kept as it was before kickoff
    assert by_game[("ATL", "PIT")]["winner"] == "ATL"      # still open, so refreshed
