"""Browser-facing login and consent pages.

Flow: an OAuth client calls /authorize (SDK) -> provider.authorize stores a pending request and
redirects here (/login?req=...) -> the user signs in with Google or GitHub -> /consent ->
on approval a code is issued and the browser goes back to the client's redirect_uri.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from mcp.server.auth.provider import construct_redirect_uri

from .. import service
from ..config import get_settings
from .provider import WEB_CLIENT_ID, provider

log = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates" / "pages"))

SESSION_COOKIE = "ctx_session"
NONCE_COOKIE = "ctx_oauth_nonce"
SESSION_MAX_AGE = 7 * 24 * 3600

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
GITHUB_AUTH = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


def _ser(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt=salt)


def _secure() -> bool:
    return get_settings().public_base_url.startswith("https://")


def _base() -> str:
    return get_settings().public_base_url.rstrip("/")


def session_user(request: Request) -> uuid.UUID | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return uuid.UUID(_ser("session").loads(raw, max_age=SESSION_MAX_AGE)["uid"])
    except (BadSignature, KeyError, ValueError):
        return None


def _set_session(resp: Response, user_id: uuid.UUID) -> None:
    resp.set_cookie(
        SESSION_COOKIE,
        _ser("session").dumps({"uid": str(user_id)}),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_secure(),
        samesite="lax",
    )


def _page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    s = get_settings()
    return templates.TemplateResponse(
        request,
        name,
        {
            "google": bool(s.google_client_id),
            "github": bool(s.github_client_id),
            "dev_login": s.dev_login and not s.is_prod,
            **ctx,
        },
        status_code=status,
    )


def _error(request: Request, message: str, status: int = 400) -> HTMLResponse:
    return _page(request, "message.html", status=status, title="Sign-in problem", message=message)


@router.get("/login")
async def login(request: Request, req: str) -> Response:
    pending = await provider.load_pending(req)
    if pending is None:
        return _error(request, "This sign-in request expired. Start again from your app.")
    if session_user(request):
        return RedirectResponse(f"/consent?req={req}", status_code=303)
    client, _ = pending
    return _page(request, "login.html", req=req, client_name=client.client_name or "An app")


def _start(provider_name: str, req: str) -> Response:
    nonce = secrets.token_urlsafe(16)
    state = _ser("oauth-state").dumps({"req": req, "nonce": nonce})
    s = get_settings()
    if provider_name == "google":
        url = (
            GOOGLE_AUTH
            + "?"
            + urlencode(
                {
                    "client_id": s.google_client_id,
                    "redirect_uri": f"{_base()}/auth/google/callback",
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": state,
                    "prompt": "select_account",
                }
            )
        )
    else:
        url = (
            GITHUB_AUTH
            + "?"
            + urlencode(
                {
                    "client_id": s.github_client_id,
                    "redirect_uri": f"{_base()}/auth/github/callback",
                    "scope": "read:user user:email",
                    "state": state,
                    "allow_signup": "true",
                }
            )
        )
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(
        NONCE_COOKIE, nonce, max_age=600, httponly=True, secure=_secure(), samesite="lax"
    )
    return resp


@router.get("/auth/{idp}/start")
async def start(request: Request, idp: str, req: str) -> Response:
    s = get_settings()
    if (idp == "google" and s.google_client_id) or (idp == "github" and s.github_client_id):
        return _start(idp, req)
    return _error(request, "That sign-in method is not configured.", 404)


def _check_state(request: Request, state: str) -> str | None:
    try:
        data = _ser("oauth-state").loads(state, max_age=600)
    except BadSignature:
        return None
    if not secrets.compare_digest(data.get("nonce", ""), request.cookies.get(NONCE_COOKIE, "")):
        return None
    return data["req"]


async def _finish(
    request: Request, req: str, subject: str, email: str, name: str | None
) -> Response:
    user_id = await service.upsert_user(subject, email, name)
    resp = RedirectResponse(f"/consent?req={req}", status_code=303)
    _set_session(resp, user_id)
    resp.delete_cookie(NONCE_COOKIE)
    return resp


@router.get("/auth/google/callback")
async def google_callback(
    request: Request, state: str, code: str | None = None, error: str | None = None
) -> Response:
    req = _check_state(request, state)
    if req is None or error or not code:
        return _error(request, "Google sign-in was cancelled or could not be verified.")
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as http:
        tok = await http.post(
            GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": f"{_base()}/auth/google/callback",
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            log.warning("google token exchange failed: %s", tok.text[:300])
            return _error(request, "Google sign-in failed. Try again.")
        info = await http.get(
            GOOGLE_USERINFO, headers={"Authorization": f"Bearer {tok.json()['access_token']}"}
        )
    u = info.json()
    if not u.get("email") or not u.get("email_verified"):
        return _error(request, "Your Google account has no verified email address.")
    return await _finish(request, req, f"google:{u['sub']}", u["email"], u.get("name"))


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, state: str, code: str | None = None, error: str | None = None
) -> Response:
    req = _check_state(request, state)
    if req is None or error or not code:
        return _error(request, "GitHub sign-in was cancelled or could not be verified.")
    s = get_settings()
    async with httpx.AsyncClient(timeout=15) as http:
        tok = await http.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "code": code,
                "client_id": s.github_client_id,
                "client_secret": s.github_client_secret,
                "redirect_uri": f"{_base()}/auth/github/callback",
            },
        )
        access = tok.json().get("access_token") if tok.status_code == 200 else None
        if not access:
            return _error(request, "GitHub sign-in failed. Try again.")
        h = {"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"}
        user = (await http.get(f"{GITHUB_API}/user", headers=h)).json()
        emails = (await http.get(f"{GITHUB_API}/user/emails", headers=h)).json()
    primary = next(
        (
            e["email"]
            for e in emails
            if isinstance(e, dict) and e.get("primary") and e.get("verified")
        ),
        None,
    )
    if not primary:
        return _error(request, "Your GitHub account has no verified primary email.")
    return await _finish(
        request, req, f"github:{user['id']}", primary, user.get("name") or user.get("login")
    )


@router.post("/auth/dev")
async def dev_login(request: Request, req: str = Form(...), email: str = Form(...)) -> Response:
    s = get_settings()
    if not s.dev_login or s.is_prod:
        return _error(request, "Not available.", 404)
    return await _finish(request, req, f"dev:{email.lower()}", email.lower(), None)


@router.get("/consent")
async def consent(request: Request, req: str) -> Response:
    user_id = session_user(request)
    if user_id is None:
        return RedirectResponse(f"/login?req={req}", status_code=303)
    pending = await provider.load_pending(req)
    if pending is None:
        return _error(request, "This sign-in request expired. Start again from your app.")
    client, params = pending
    if client.client_id == WEB_CLIENT_ID:
        return await _approve(req, user_id, params)
    user = await service.get_user(user_id)
    csrf = _ser("consent").dumps({"req": req, "uid": str(user_id)})
    return _page(
        request,
        "consent.html",
        req=req,
        csrf=csrf,
        user=user,
        client_name=client.client_name or "An app",
        redirect_host=str(params.redirect_uri).split("/")[2],
    )


async def _approve(req: str, user_id: uuid.UUID, params) -> Response:
    code = await provider.issue_code(req, user_id)
    if code is None:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(
        construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
        status_code=303,
    )


@router.post("/consent")
async def consent_post(
    request: Request, req: str = Form(...), csrf: str = Form(...), decision: str = Form(...)
) -> Response:
    user_id = session_user(request)
    try:
        data = _ser("consent").loads(csrf, max_age=900)
    except BadSignature:
        data = {}
    if user_id is None or data.get("req") != req or data.get("uid") != str(user_id):
        return _error(request, "Your session expired. Start again from your app.")
    pending = await provider.load_pending(req)
    if pending is None:
        return _error(request, "This sign-in request expired. Start again from your app.")
    _, params = pending
    if decision != "approve":
        await provider.deny(req)
        return RedirectResponse(
            construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            ),
            status_code=303,
        )
    return await _approve(req, user_id, params)


@router.get("/privacy")
async def privacy(request: Request) -> Response:
    return _page(request, "privacy.html")


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
