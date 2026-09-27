"""Logs are structured, correlated, and never carry draft text, credentials, or personal data."""
import io
import json
import logging

from app import logs
from tests.support import make_campaign, run_research_flow

SECRET_KEY = "sk-proj-THISISAFAKEKEY1234567890abcdef"


def test_redact_scrubs_sensitive_values():
    text = (f'api_key={SECRET_KEY} {{"access_token": "ya29.a0AfH6SMBx", "refresh_token": "1//0gAbCdEfGhIjKlMn"}} '  # gitleaks:allow (fake fixture)
            "Authorization: Bearer abc.def.ghi mail avery.lin@demo.example.edu call +1 (732) 555-0199 "
            'body="Dear Dr. Lin, I loved your paper" session=Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4cXV1eA')
    out = logs.redact(text)
    for leak in ("sk-proj", "ya29.", "1//0g", "abc.def", "avery.lin", "555-0199", "Dear Dr", "Zm9vYmFy"):
        assert leak not in out, leak
    assert logs.redact("job done campaign cmp_20260927_0b12aba6") == "job done campaign cmp_20260927_0b12aba6"


def test_workflow_logs_are_json_correlated_and_clean(env):
    client, svc, fake = env
    buf = io.StringIO()
    handler = logs.setup("INFO", buf)
    try:
        cid = make_campaign(client, "Rutgers/Princeton professors working on computational neurodevelopment")
        ids = run_research_flow(client, cid)
        d = client.get(f"/api/campaigns/{cid}/candidates/{ids[0]}").json()["draft"]
        client.post(f"/api/campaigns/{cid}/drafts/{ids[0]}/approve")
        client.post(f"/api/campaigns/{cid}/gmail-drafts", json={"candidate_ids": [ids[0]]})
        # an exception whose message carries secrets and personal data must still come out clean
        try:
            raise RuntimeError(f"provider said: key={SECRET_KEY} for avery.lin@demo.example.edu body={d['body']!r}")
        except RuntimeError:
            logging.getLogger("jobs").exception("job failed")
        r = client.get("/api/campaigns", headers={"x-request-id": "req-abc12345"})
        assert r.headers["x-request-id"] == "req-abc12345"
    finally:
        logging.getLogger().removeHandler(handler)

    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert any(line.get("request_id") == "req-abc12345" and line["msg"] == "request" for line in lines)
    jobs = [line for line in lines if line["msg"] == "job done"]
    assert jobs and all(line.get("job_id") and line.get("request_id") for line in jobs)
    dump = buf.getvalue()
    profile = svc.store.research(cid)["profiles"][ids[0]]
    sensitive = [SECRET_KEY, "avery.lin", profile["contact_email"], d["subject"], d["body"][:40],
                 *(line for line in d["body"].splitlines() if len(line) > 25), "?t=", "x-app-token"]
    for s in sensitive:
        assert s not in dump, s
