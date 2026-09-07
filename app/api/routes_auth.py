"""Login/logout pages and the Settings -> Authentication management API.
See app/auth.py for hashing, session-cookie, and lockout details."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, config
from .deps import get_config

router = APIRouter()
settings_router = APIRouter(prefix="/api/auth")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(next: str) -> str:
    return next if next.startswith("/") and not next.startswith("//") else "/"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", settings=Depends(get_config)):
    if not settings.auth_password_hash:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "next": _safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
    settings=Depends(get_config),
):
    next = _safe_next(next)
    lockout: auth.LoginLockout = request.app.state.login_lockout
    key = _client_key(request)
    if lockout.locked(key):
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "next": next, "error": "Too many attempts — try again in a few minutes."},
            status_code=429,
        )
    if not auth.verify_password(password, settings.auth_password_hash):
        lockout.record_failure(key)
        return templates.TemplateResponse(
            request, "login.html",
            {"request": request, "next": next, "error": "Incorrect password."},
            status_code=401,
        )
    lockout.clear(key)
    signer: auth.SessionSigner = request.app.state.session_signer
    token = signer.issue()
    resp = RedirectResponse(next, status_code=302)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_MAX_AGE,
        httponly=True, samesite="lax", secure=secure,
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@settings_router.post("/password")
async def update_password(
    new_password: str = Form(""),
    current_password: str = Form(""),
    disable: bool = Form(False),
    settings=Depends(get_config),
):
    """First call with no password configured sets one with no current-password
    check; every later call (change or disable) requires the current one."""
    if settings.auth_password_hash and not auth.verify_password(current_password, settings.auth_password_hash):
        raise HTTPException(401, "current password is incorrect")

    if disable:
        config.save_overrides(settings.state_dir, auth_password_hash="")
        settings.auth_password_hash = ""
        return {"ok": True, "configured": False}

    if len(new_password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    new_hash = auth.hash_password(new_password)
    config.save_overrides(settings.state_dir, auth_password_hash=new_hash)
    settings.auth_password_hash = new_hash
    return {"ok": True, "configured": True}


@settings_router.post("/token/regenerate")
def regenerate_token(settings=Depends(get_config)):
    if not settings.auth_password_hash:
        raise HTTPException(400, "set a login password first")
    token = auth.generate_token()
    config.save_overrides(settings.state_dir, auth_api_token=token)
    settings.auth_api_token = token
    return {"ok": True, "token": token}


@settings_router.post("/token/clear")
def clear_token(settings=Depends(get_config)):
    config.save_overrides(settings.state_dir, auth_api_token="")
    settings.auth_api_token = ""
    return {"ok": True}


@settings_router.post("/signout-all")
def signout_all(request: Request):
    signer: auth.SessionSigner = request.app.state.session_signer
    signer.bump_secret()
    return {"ok": True}
