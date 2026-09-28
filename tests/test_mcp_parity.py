import inspect
import re

from app import mcp_server
from app.api import create_app

from test_contacts import boot

FORBIDDEN_NAMES = (
    "approve_draft",
    "approve_followup",
    "confirm_send",
    "send_now",
    "enable_sending",
    "unpause_sending",
    "update_sending_settings",
    "set_openai_key",
    "disconnect_gmail",
    "delete_campaign",
)
FORBIDDEN_PATHS = ("/approve", "/sending/settings", "/sending/unpause", "/account", "/delete", "/auth", "/confirm")
NEW_TOOLS = (
    "resolve_contact_review create_identity update_identity delete_identity followup_queue followup_sequence "
    "followup_step regenerate_followups gmail_sync record_outcome delete_outcome analytics_report "
    "list_notifications update_notification"
).split()


def test_renamed_to_hermes():
    assert mcp_server.mcp.name == "hermes"
    assert "outreach desk" not in inspect.getsource(mcp_server).lower()
    assert "HERMES is not running" in mcp_server.call.__code__.co_consts


def test_prohibited_actions_absent():
    for name in FORBIDDEN_NAMES:
        assert not hasattr(mcp_server, name)
    calls = re.findall(r'call\(\s*"[A-Z]+",\s*f?"([^"]*)"', inspect.getsource(mcp_server))
    for path in calls:
        assert not any(path.startswith(f) for f in FORBIDDEN_PATHS), path
        assert not path.endswith(("/approve", "/confirm")), path


def test_new_tools_hit_real_routes_and_errors_stay_structured(tmp_path, monkeypatch):
    client, svc = boot(tmp_path / "data")
    routes = [(r.methods, r.path_regex) for r in create_app(service=svc, token="t").routes if hasattr(r, "methods")]
    seen = []

    def fake(method, url, json=None, params=None, **kw):
        path = url.replace(mcp_server.BASE, "/api")
        seen.append((method, path))
        return client.request(method, path, json=json, params=params)

    monkeypatch.setattr(mcp_server.httpx, "request", fake)
    with client:
        calls = [
            mcp_server.resolve_contact_review(999),
            mcp_server.create_identity({"name": "T"}),
            mcp_server.update_identity(999, {"name": "U"}),
            mcp_server.delete_identity(999),
            mcp_server.followup_queue("nope"),
            mcp_server.followup_sequence("nope", "c", "pause"),
            mcp_server.followup_step("nope", "c", 0, "skip"),
            mcp_server.regenerate_followups("nope", ["c"]),
            mcp_server.gmail_sync("nope"),
            mcp_server.record_outcome("nope", "c", "replied"),
            mcp_server.delete_outcome("nope", "c", "replied"),
            mcp_server.analytics_report(),
            mcp_server.analytics_report(csv_kind="aggregate"),
            mcp_server.list_notifications(),
            mcp_server.update_notification(action="read_all"),
            mcp_server.update_notification(1, "dismiss"),
        ]
    assert len(seen) == len(calls)
    for method, path in seen:
        assert any(method in m and rx.match(path) for m, rx in routes), (method, path)
    for res in calls:
        assert isinstance(res, dict)
    assert "error" in calls[4] and "error" in calls[8]


def test_human_only_step_actions_rejected_before_any_request(monkeypatch):
    monkeypatch.setattr(mcp_server, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called API")))
    assert "error" in mcp_server.followup_step("c", "p", 0, "approve")
    assert "error" in mcp_server.followup_step("c", "p", 0, "edit")
    assert "error" in mcp_server.followup_sequence("c", "p", "mark_sent")
    assert "error" in mcp_server.followup_sequence("c", "p", "do_not_contact")
    assert "error" in mcp_server.update_notification(1, "bogus")
    assert {n for n in NEW_TOOLS if not callable(getattr(mcp_server, n))} == set()
