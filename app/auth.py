"""Password hashing, signed session cookies, API tokens, and login lockout
for the dashboard. Everything here is inert until a password hash is
configured (see Settings -> Technical -> Authentication) — Wharf ships with
no login wall by default, matching its usual trust model."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_COOKIE = "wharf_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_S = 300.0  # 5 minutes

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "testclient"}
EXEMPT_PATHS = {"/login", "/logout", "/healthz"}
EXEMPT_PREFIXES = ("/static/", "/launch/")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        salt_hex, digest_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _secret_path(state_dir: Path) -> Path:
    return state_dir / "auth_secret.key"


def _write_secret(state_dir: Path, secret: bytes) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _secret_path(state_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(secret.hex())
    os.replace(tmp, path)


class SessionSigner:
    """Stateless signed session cookies: `<expiry>.<hmac>` over a secret
    generated once into state_dir/auth_secret.key, so logins survive a
    container restart. Regenerating that secret (bump_secret) invalidates
    every outstanding cookie at once — the "sign out everywhere" action."""

    def __init__(self, state_dir: Path):
        self._state_dir = state_dir
        try:
            self._secret = bytes.fromhex(_secret_path(state_dir).read_text().strip())
        except (OSError, ValueError):
            self._secret = secrets.token_bytes(32)
            _write_secret(state_dir, self._secret)

    def issue(self, max_age: int = SESSION_MAX_AGE) -> str:
        expiry = int(time.time()) + max_age
        mac = hmac.new(self._secret, str(expiry).encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{mac}"

    def verify(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        expiry_s, _, mac = token.partition(".")
        try:
            expiry = int(expiry_s)
        except ValueError:
            return False
        if expiry < time.time():
            return False
        expected = hmac.new(self._secret, expiry_s.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(mac, expected)

    def bump_secret(self) -> None:
        self._secret = secrets.token_bytes(32)
        _write_secret(self._state_dir, self._secret)


class LoginLockout:
    """In-memory brute-force guard: N failures from the same source within
    a window blocks further attempts until the window elapses. Reset on
    process restart, which is fine — the point is slowing down an online
    guessing attack, not surviving a restart."""

    def __init__(self, threshold: int = LOCKOUT_THRESHOLD, window_s: float = LOCKOUT_WINDOW_S):
        self._threshold = threshold
        self._window_s = window_s
        self._failures: dict[str, list[float]] = {}

    def locked(self, key: str) -> bool:
        self._prune(key)
        return len(self._failures.get(key, [])) >= self._threshold

    def record_failure(self, key: str) -> None:
        self._prune(key)
        self._failures.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def _prune(self, key: str) -> None:
        if key not in self._failures:
            return
        cutoff = time.time() - self._window_s
        self._failures[key] = [t for t in self._failures[key] if t > cutoff]


class AuthMiddleware(BaseHTTPMiddleware):
    """Gates every request behind a session cookie or bearer token, except:
    requests from the same host as Wharf itself (tools already call Wharf's
    own API over loopback, e.g. the notify relay), /launch/<tool> (kept open
    for bookmarks/Shortcuts), and a small set of always-public paths. Fully
    inert until settings.auth_password_hash is set."""

    async def dispatch(self, request: Request, call_next):
        settings = request.app.state.settings
        if not settings.auth_password_hash:
            return await call_next(request)

        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        client_host = request.client.host if request.client else None
        if client_host in LOOPBACK_HOSTS:
            return await call_next(request)

        signer: SessionSigner = request.app.state.session_signer
        if signer.verify(request.cookies.get(SESSION_COOKIE)):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and settings.auth_api_token:
            token = auth_header[len("Bearer "):]
            if hmac.compare_digest(token, settings.auth_api_token):
                return await call_next(request)

        if request.headers.get("hx-request"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/login?next={quote(path + query, safe='')}", status_code=302)
        return JSONResponse({"detail": "not authenticated"}, status_code=401)
