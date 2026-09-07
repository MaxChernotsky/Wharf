import time

from app import auth


def test_hash_and_verify_password_roundtrip():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed)
    assert not auth.verify_password("wrong password", hashed)


def test_verify_password_rejects_empty_or_malformed_hash():
    assert not auth.verify_password("anything", "")
    assert not auth.verify_password("anything", "not-a-valid-hash")


def test_hash_password_is_salted():
    assert auth.hash_password("same password") != auth.hash_password("same password")


def test_generate_token_is_unique_and_urlsafe():
    a, b = auth.generate_token(), auth.generate_token()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_session_signer_issue_and_verify_roundtrip(tmp_path):
    signer = auth.SessionSigner(tmp_path)
    token = signer.issue()
    assert signer.verify(token)


def test_session_signer_rejects_missing_or_malformed_token(tmp_path):
    signer = auth.SessionSigner(tmp_path)
    assert not signer.verify(None)
    assert not signer.verify("")
    assert not signer.verify("no-dot-here")
    assert not signer.verify("not-an-int.deadbeef")


def test_session_signer_rejects_expired_token(tmp_path):
    signer = auth.SessionSigner(tmp_path)
    token = signer.issue(max_age=-1)
    assert not signer.verify(token)


def test_session_signer_rejects_tampered_token(tmp_path):
    signer = auth.SessionSigner(tmp_path)
    token = signer.issue()
    expiry, _, mac = token.partition(".")
    assert not signer.verify(f"{expiry}.{mac[:-1]}f")
    assert not signer.verify(f"{int(expiry) + 1000}.{mac}")


def test_session_signer_persists_secret_across_instances(tmp_path):
    signer1 = auth.SessionSigner(tmp_path)
    token = signer1.issue()
    signer2 = auth.SessionSigner(tmp_path)
    assert signer2.verify(token)


def test_bump_secret_invalidates_old_tokens(tmp_path):
    signer = auth.SessionSigner(tmp_path)
    token = signer.issue()
    signer.bump_secret()
    assert not signer.verify(token)
    new_token = signer.issue()
    assert signer.verify(new_token)


def test_lockout_allows_attempts_under_threshold():
    lockout = auth.LoginLockout(threshold=3, window_s=60)
    lockout.record_failure("1.2.3.4")
    lockout.record_failure("1.2.3.4")
    assert not lockout.locked("1.2.3.4")


def test_lockout_blocks_at_threshold():
    lockout = auth.LoginLockout(threshold=3, window_s=60)
    for _ in range(3):
        lockout.record_failure("1.2.3.4")
    assert lockout.locked("1.2.3.4")


def test_lockout_is_per_key():
    lockout = auth.LoginLockout(threshold=1, window_s=60)
    lockout.record_failure("1.2.3.4")
    assert lockout.locked("1.2.3.4")
    assert not lockout.locked("5.6.7.8")


def test_lockout_clear_resets():
    lockout = auth.LoginLockout(threshold=1, window_s=60)
    lockout.record_failure("1.2.3.4")
    assert lockout.locked("1.2.3.4")
    lockout.clear("1.2.3.4")
    assert not lockout.locked("1.2.3.4")


def test_lockout_prunes_failures_outside_window():
    lockout = auth.LoginLockout(threshold=1, window_s=0.05)
    lockout.record_failure("1.2.3.4")
    assert lockout.locked("1.2.3.4")
    time.sleep(0.1)
    assert not lockout.locked("1.2.3.4")
