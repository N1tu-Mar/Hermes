import pytest

from app.outlines import OutlineBlocked, build_outline, route_template
from app.writer import check_draft
from tests.support import BIO


def test_route_template():
    assert route_template({"mode": "research", "subtype": "research_professor"}) == "research_professor"
    assert route_template({"mode": "outreach", "subtype": "startup"}) == "startup"
    assert route_template({"mode": "outreach", "subtype": "speaker_mentor"}) == "speaker_invite"
    assert route_template({"mode": "outreach", "subtype": "speaker_mentor"}, followup=True) == "rsvp_followup"
    with pytest.raises(OutlineBlocked):
        route_template({"mode": "outreach", "subtype": "startup"}, followup=True)


def test_followup_blocked_without_recorded_invite():
    prof = {"name": "A B", "evidence": [{"claim": "x", "source_url": "https://a.org/p"}], "fit_reason": ""}
    with pytest.raises(OutlineBlocked):
        build_outline({"mode": "outreach", "subtype": "speaker_mentor"}, prof, BIO, followup=True)


def test_draft_checks_flag_invented_details():
    outline = {
        "evidence": [{"claim": "Studies infant attention."}],
        "sender_context": BIO,
        "event_details": None,
        "earlier_invite": None,
        "ask": "a chat",
        "maximum_length": 150,
    }
    issues = check_draft(
        "Hi", "As we discussed on March 3, see https://x.io and your 2019 paper.", outline, None, ["e0"]
    )
    joined = " ".join(issues)
    assert "raw URL" in joined and "date" in joined and "year" in joined and "prior relationship" in joined
    assert check_draft("Hi", "I read that you study infant attention.", outline, None, ["e0"]) == []
