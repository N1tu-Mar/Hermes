import json

from app.logs import redact
from app.operational_metrics import Metrics


def test_snapshot_signals(tmp_path):
    m = Metrics(clock=lambda: 1000.0)
    for s in (0.1, 0.2, 0.3):
        m.provider_call(s, retries=1)
    m.sqlite_call(0.005)
    m.uncertain_send()
    snap = m.snapshot(
        queues={"research": [900.0, 990.0], "write": []},
        gmail_last_sync=940.0,
        data_root=tmp_path,
        services=["api", "worker", "api"],
        backup_verified_at=100.0,
    )
    assert snap["queues"] == {"research": {"depth": 2, "oldest_age_s": 100.0}, "write": {"depth": 0, "oldest_age_s": 0}}
    assert snap["provider"]["retries"] == 3 and snap["provider"]["count"] == 3 and snap["provider"]["p95_ms"] == 300.0
    assert snap["sqlite"]["max_ms"] == 5.0 and snap["uncertain_sends"] == 1
    assert snap["gmail_sync_age_s"] == 60.0 and snap["backup_verified_age_s"] == 900.0
    assert snap["services"] == {"active": 2, "names": ["api", "worker"]}
    assert snap["disk"]["free_bytes"] > 0 and 0 <= snap["disk"]["used_pct"] <= 100


def test_unknowns_are_none_and_nothing_leaks(tmp_path):
    snap = Metrics().snapshot(
        queues={"ann@example.com": [1.0], "https://x.test/p": [1.0], "ok": [1.0]},
        services=["bob@example.com", "http://s.test", "svc"],
    )
    text = json.dumps(snap)
    assert "@" not in text and "http" not in text and "example" not in text
    assert list(snap["queues"]) == ["ok"] and snap["services"]["names"] == ["svc"]
    assert snap["gmail_sync_age_s"] is None and snap["backup_verified_age_s"] is None and snap["disk"] is None


def test_time_sqlite_records():
    m = Metrics()
    with m.time_sqlite():
        pass
    assert m.snapshot()["sqlite"]["count"] == 1


def test_logs_redact_source_urls():
    out = redact("fetch failed https://user:pw@site.test/a?code=1&x=2 then https://b.test")
    assert "site.test" not in out and "b.test" not in out and "[url]" in out
