"""Durable, bounded login throttling: account/source/global limits, restart survival,
bounded state under many distinct usernames, concurrency safety, input-length
validation, and safe scrypt-parameter migration."""

import threading

import pytest
from cryptography.fernet import Fernet

from app import auth
from app.auth import MAX_FAILURE_ROWS, MAX_FAILURES, SOURCE_MAX_FAILURES, Accounts

PW = "correct-horse-battery-123"


@pytest.fixture
def acc(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    key = Fernet.generate_key().decode()
    a = Accounts(root, key)
    a.add_user("alice", PW)
    yield a
    a.close()


def test_account_lockout_after_max_failures(acc):
    for _ in range(MAX_FAILURES):
        assert acc.login("alice", "wrong") is None
    # threshold reached: even the correct password is now refused
    assert acc.login("alice", PW) is None
    status = acc.login_status("alice")
    assert status["locked"] is True
    assert status["retry_after"] > 0


def test_lockout_does_not_affect_other_accounts(acc):
    acc.add_user("bob", "bob-password-4567")
    for _ in range(MAX_FAILURES):
        assert acc.login("alice", "wrong") is None
    assert acc.login("bob", "bob-password-4567") is not None


def test_source_scope_limits_independent_of_account(acc):
    for i in range(MAX_FAILURES):
        acc.add_user(f"victim{i}", "victim-password-000")
    for i in range(SOURCE_MAX_FAILURES):
        acc.login(f"victim{i % MAX_FAILURES}", "wrong", source="1.2.3.4")
    status = acc.login_status("someone-else-entirely", source="1.2.3.4")
    assert status["locked"] is True
    # a different source is unaffected
    assert acc.login_status("someone-else-entirely", source="9.9.9.9")["locked"] is False


def test_successful_login_clears_account_counter_but_not_source(acc):
    acc.add_user("carol", "carol-password-000")
    for _ in range(MAX_FAILURES - 1):
        acc.login("alice", "wrong", source="5.5.5.5")
    assert acc.login("alice", PW, source="5.5.5.5") is not None
    # account counter reset, immediate re-login works
    assert acc.login("alice", PW, source="5.5.5.5") is not None


def test_restart_preserves_lockout(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    key = Fernet.generate_key().decode()
    a1 = Accounts(root, key)
    a1.add_user("alice", PW)
    for _ in range(MAX_FAILURES):
        assert a1.login("alice", "wrong") is None
    a1.close()

    a2 = Accounts(root, key)
    try:
        assert a2.login("alice", PW) is None
        assert a2.login_status("alice")["locked"] is True
    finally:
        a2.close()


def test_bounded_state_under_many_unique_bogus_usernames(acc):
    for i in range(3000):
        acc.login(f"bogus-user-{i}", "wrong")
    rows = acc.q("SELECT COUNT(*) AS n FROM login_failures")
    assert rows[0]["n"] <= MAX_FAILURE_ROWS


def test_oversized_password_rejected_before_hashing(acc, monkeypatch):
    called = []
    monkeypatch.setattr(auth, "check_password", lambda *a, **k: called.append(1) or False)
    huge = "x" * (auth.MAX_PASSWORD + 1)
    assert acc.login("alice", huge) is None
    assert called == []  # never reached the expensive hash comparison


def test_oversized_username_rejected(acc):
    huge = "u" * (auth.MAX_USERNAME + 1)
    assert acc.login(huge, PW) is None


def test_add_user_rejects_oversized_password(acc):
    with pytest.raises(ValueError):
        acc.add_user("dave", "x" * (auth.MAX_PASSWORD + 1))


def test_foreign_key_cascade_on_user_delete(acc):
    row = acc.q("SELECT id FROM users WHERE username='alice'")[0]
    uid = row["id"]
    tok, csrf, _ = acc.login("alice", PW)
    acc.put_secret(uid, "openai_api_key", "sk-test-0123456789abcdef")
    assert acc.q("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (uid,))[0]["n"] == 1
    assert acc.q("SELECT COUNT(*) AS n FROM secrets WHERE user_id=?", (uid,))[0]["n"] == 1
    acc.delete_user("alice")
    assert acc.q("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (uid,))[0]["n"] == 0
    assert acc.q("SELECT COUNT(*) AS n FROM secrets WHERE user_id=?", (uid,))[0]["n"] == 0


def test_password_hash_migrated_to_current_params_on_login(acc):
    row = acc.q("SELECT id, pw_hash FROM users WHERE username='alice'")[0]
    old_params = {"n": 2**10, "r": 8, "p": 1}  # deliberately weaker than SCRYPT_PARAMS
    weak_hash = auth.hash_password(PW, params=old_params)
    acc.x("UPDATE users SET pw_hash=? WHERE id=?", (weak_hash, row["id"]))
    assert acc.login("alice", PW) is not None
    new_hash = acc.q("SELECT pw_hash FROM users WHERE id=?", (row["id"],))[0]["pw_hash"]
    assert new_hash != weak_hash
    assert not auth._needs_rehash(new_hash)
    # still verifiable with the new hash
    assert auth.check_password(PW, new_hash)


def test_cleanup_expired_is_independent_of_login(acc, monkeypatch):
    tok, csrf, uid = acc.login("alice", PW)
    acc.q("SELECT seen_at FROM sessions WHERE token_hash IS NOT NULL")[0]
    acc.x("UPDATE sessions SET seen_at=?, created_at=? WHERE user_id=?", (0, 0, uid))
    # login() itself no longer sweeps sessions
    acc.login("alice", "wrong")
    assert acc.q("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (uid,))[0]["n"] == 1
    deleted = acc.cleanup_expired()
    assert deleted == 1
    assert acc.q("SELECT COUNT(*) AS n FROM sessions WHERE user_id=?", (uid,))[0]["n"] == 0


def test_concurrent_logins_cannot_exceed_account_threshold(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    key = Fernet.generate_key().decode()
    a = Accounts(root, key)
    a.add_user("alice", PW)
    try:
        barrier = threading.Barrier(50)
        successes = []

        def attempt():
            barrier.wait()
            if a.login("alice", "wrong") is not None:
                successes.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not successes
        row = a.q("SELECT count FROM login_failures WHERE scope='account' AND key='alice'")[0]
        assert row["count"] == 50  # every failed attempt counted exactly once, no lost updates
        assert a.login("alice", PW) is None  # threshold (5) was crossed well before 50
        assert a.login_status("alice")["locked"] is True
    finally:
        a.close()


def test_global_scope_locks_out_unrelated_accounts_after_flood(acc):
    for i in range(auth.GLOBAL_MAX_FAILURES):
        acc.add_user(f"flood{i}", "flood-password-000")
    for i in range(auth.GLOBAL_MAX_FAILURES):
        acc.login(f"flood{i}", "wrong")
    acc.add_user("zed", "zed-password-0000")
    assert acc.login("zed", "zed-password-0000") is None
    assert acc.login_status("zed")["locked"] is True
