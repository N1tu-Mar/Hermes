from app.research import finalize_profile


def test_unsourced_claims_and_unverified_email_rejected():
    cand = {"candidate_id": "c_001", "name": "A", "organization": "O", "role": "R"}
    data = {
        "contact_email": "a@o.edu",
        "contact_source_url": "https://o.edu/a",
        "summary": "s",
        "research_interests": [],
        "fit_reason": "f",
        "evidence": [
            {"claim": "sourced", "source_url": "https://o.edu/a"},
            {"claim": "made up", "source_url": "https://nowhere.example/x"},
            {"claim": "no url", "source_url": ""},
        ],
    }
    p = finalize_profile(cand, data, known_urls={"https://o.edu/a"}, pages={"https://o.edu/a": "no email here"})
    assert [e["claim"] for e in p["evidence"]] == ["sourced"]
    assert p["email_verified_on_page"] is False and p["status"] == "needs_contact_review"
    p = finalize_profile(cand, data, known_urls={"https://o.edu/a"}, pages={"https://o.edu/a": "Email: a@o.edu"})
    assert p["email_verified_on_page"] is True and p["status"] == "researched"
