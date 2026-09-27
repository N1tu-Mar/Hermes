from app.campaigns import parse_request


def test_parse_request():
    intake, q = parse_request(
        "Rutgers/Princeton professors working on computational neurodevelopment who may work with undergraduates"
    )
    assert intake["subtype"] == "research_professor"
    assert {"Rutgers", "Princeton"} <= set(intake["organizations"])
    assert intake["research_areas"] == ["computational neurodevelopment"]
    assert q is None
