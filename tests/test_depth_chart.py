from depth_chart import match_rating_name, normalize, top_quarterback

QB_NAMES = ["K.Cousins", "M.Penix", "T.Tagovailoa", "A.St. Brown", "D.Thompson-Robinson", "P.Mahomes"]


def chart(*athletes):
    return {"depthchart": [
        {"name": "Base 3-4 D", "positions": {"lde": {"athletes": [{"displayName": "Someone Else"}]}}},
        {"name": "3WR 1TE", "positions": {"qb": {"athletes": [
            {"id": str(i), "displayName": full, "shortName": short, "injuries": injuries}
            for i, (full, short, injuries) in enumerate(athletes)
        ]}}},
    ]}


def test_top_quarterback_is_the_first_name_in_the_qb_slot():
    top = top_quarterback(chart(("Tua Tagovailoa", "T. Tagovailoa", []), ("Michael Penix Jr.", "M. Penix Jr.", [])))
    assert top["name"] == "Tua Tagovailoa" and top["shortName"] == "T. Tagovailoa" and top["status"] is None


def test_top_quarterback_carries_the_injury_status_and_survives_an_empty_slot():
    top = top_quarterback(chart(("Patrick Mahomes", "P. Mahomes", [{"status": "Questionable"}])))
    assert top["status"] == "Questionable"
    assert top_quarterback(chart()) is None
    assert top_quarterback({"depthchart": []}) is None


def test_names_match_the_play_by_play_spelling_across_suffixes_dots_and_hyphens():
    assert normalize("M. Penix Jr.") == normalize("M.Penix")
    assert normalize("A. St. Brown") == normalize("A.St. Brown")
    assert match_rating_name("T. Tagovailoa", QB_NAMES) == "T.Tagovailoa"
    assert match_rating_name("M. Penix Jr.", QB_NAMES) == "M.Penix"
    assert match_rating_name("D. Thompson-Robinson", QB_NAMES) == "D.Thompson-Robinson"
    assert match_rating_name("C.J. Stroud", QB_NAMES + ["C.Stroud"]) == "C.Stroud"
    assert match_rating_name("J. Strand", QB_NAMES) is None      # a rookie with no plays yet
    assert match_rating_name("", QB_NAMES) is None


def test_manual_override_wins_over_the_depth_chart(tmp_path, monkeypatch):
    import depth_chart
    monkeypatch.setattr(depth_chart, "CACHE", tmp_path / "depth-charts.json")
    feeds = {"1": chart(("Tua Tagovailoa", "T. Tagovailoa", [])), "12": chart(("Patrick Mahomes", "P. Mahomes", []))}
    def get_json(url):
        team_id = url.rstrip("/").split("/")[-2]
        if team_id not in feeds:
            raise RuntimeError("down")
        return feeds[team_id]
    starters, problems = depth_chart.projected_starters({"1": "ATL", "12": "KC", "7": "DEN"}, get_json, QB_NAMES, {"KC": "G.Smith"})
    assert starters["ATL"]["ratingName"] == "T.Tagovailoa" and starters["ATL"]["source"] == "ESPN depth chart"
    assert starters["KC"]["source"] == "overrides.json" and starters["KC"]["ratingName"] == "G.Smith"
    assert "DEN" not in starters and problems == ["DEN: down"]
    assert (tmp_path / "depth-charts.json").exists()


def test_readout_names_a_new_starter_and_stays_quiet_for_the_same_man():
    from update import starter_sentences
    game = {
        "home": {"abbreviation": "PIT", "name": "Pittsburgh Steelers"}, "away": {"abbreviation": "ATL", "name": "Atlanta Falcons"},
        "starters": {
            "away": {"name": "Tua Tagovailoa", "ratingName": "T.Tagovailoa", "lastSeason": "K.Cousins", "source": "ESPN depth chart", "status": None},
            "home": {"name": "Aaron Rodgers", "ratingName": "A.Rodgers", "lastSeason": "A.Rodgers", "source": "ESPN depth chart", "status": None},
        },
    }
    assert starter_sentences(game) == ["Atlanta Falcons starts Tua Tagovailoa, per the depth chart, not K. Cousins, who took most of last season's snaps."]
    game["starters"]["home"] = {"name": "Will Howard", "ratingName": None, "lastSeason": "A.Rodgers", "source": "ESPN depth chart", "status": "Out"}
    lines = starter_sentences(game)
    assert lines[1].startswith("Pittsburgh Steelers starts Will Howard, per the depth chart; he has no plays")
    assert lines[2] == "Will Howard is listed Out on the injury report."
