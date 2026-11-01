"""End to end over HTTP: OAuth (DCR + PKCE + consent), REST, and MCP tool calls."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

REDIRECT = "http://127.0.0.1:53682/callback"
_RPC_IDS = itertools.count(1)


@pytest.fixture
async def client():
    from ctx_server.app import create_app

    app = create_app()
    started, stop = asyncio.Event(), asyncio.Event()

    async def run_lifespan():
        # Enter and exit the lifespan in one task (its task group requires it).
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    await started.wait()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c
    stop.set()
    await task


async def oauth_login(c: httpx.AsyncClient, email: str) -> dict:
    reg = await c.post(
        "/register",
        json={
            "client_name": "ctx CLI",
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    r = await c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "ctx",
        },
    )
    assert r.status_code == 302, r.text
    login_url = urlparse(r.headers["location"])
    req = parse_qs(login_url.query)["req"][0]
    page = await c.get(f"/login?req={req}")
    assert "Development sign-in" in page.text
    r = await c.post("/auth/dev", data={"req": req, "email": email})
    assert r.status_code == 303 and r.headers["location"].startswith("/consent")
    page = await c.get(r.headers["location"])
    assert "Allow ctx CLI?" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = await c.post("/consent", data={"req": req, "csrf": csrf, "decision": "approve"})
    assert r.status_code == 303
    back = urlparse(r.headers["location"])
    q = parse_qs(back.query)
    assert q["state"] == ["xyz"] and back.netloc == "127.0.0.1:53682"
    tok = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200, tok.text
    # Codes are single use.
    again = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400
    c.cookies.clear()
    return {**tok.json(), "client_id": client_id}


async def mcp_call(
    c: httpx.AsyncClient, token: str, method: str, params: dict | None = None
) -> dict:
    rpc_id = next(_RPC_IDS)
    r = await c.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_metadata_and_unauthenticated(client):
    meta = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert meta["registration_endpoint"].endswith("/register")
    assert "S256" in meta["code_challenge_methods_supported"]
    prm = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200 and prm.json()["resource"].endswith("/mcp")
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    assert (await client.get("/api/v1/repos")).status_code == 401


async def test_full_flow_rest_and_mcp(client):
    tok = await oauth_login(client, "carol@example.com")
    h = {"Authorization": f"Bearer {tok['access_token']}"}

    r = await client.post("/api/v1/repos", json={"name": "llm-context-vc"}, headers=h)
    assert r.status_code == 201, r.text
    bad = await client.post(
        "/api/v1/commits",
        headers=h,
        json={
            "repo": "llm-context-vc",
            "message": "m",
            "items": [{"type": "nope", "subject": "", "content": "x"}],
        },
    )
    assert bad.status_code == 422
    assert {d["field"] for d in bad.json()["details"]} >= {"items.0.type", "items.0.subject"}

    init = await mcp_call(
        client,
        tok["access_token"],
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "ctx"
    tools = await mcp_call(client, tok["access_token"], "tools/list")
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {
        "ctx_status",
        "ctx_commit",
        "ctx_confirm",
        "ctx_log",
        "ctx_diff",
        "ctx_branch",
        "ctx_checkout",
        "ctx_merge",
        "ctx_resolve",
        "ctx_pull",
        "ctx_revert",
    } <= names

    res = await mcp_call(
        client,
        tok["access_token"],
        "tools/call",
        {
            "name": "ctx_commit",
            "arguments": {
                "repo": "llm-context-vc",
                "message": "Chose storage layer",
                "items": [
                    {
                        "type": "decision",
                        "subject": "Primary datastore",
                        "content": "Use Cloud SQL Postgres with pgvector.",
                        "reason": "One database for state and vectors",
                    },
                    {
                        "type": "open_question",
                        "subject": "ChatGPT write tools",
                        "content": "Does ChatGPT allow write tools over MCP?",
                    },
                ],
            },
        },
    )
    out = res["result"]["structuredContent"]
    assert out["committed"] is True

    res = await mcp_call(
        client,
        tok["access_token"],
        "tools/call",
        {
            "name": "ctx_pull",
            "arguments": {"repo": "llm-context-vc", "target_model": "claude", "token_budget": 1000},
        },
    )
    ctx = res["result"]["structuredContent"]["context"]
    assert "<decisions>" in ctx and "pgvector" in ctx

    # Domain errors come back as tool errors the model can read.
    res = await mcp_call(
        client,
        tok["access_token"],
        "tools/call",
        {"name": "ctx_status", "arguments": {"repo": "does-not-exist"}},
    )
    assert res["result"]["isError"] is True
    assert "not_found" in res["result"]["content"][0]["text"]

    # Another user cannot see the repo.
    dave = await oauth_login(client, "dave@example.com")
    r = await client.get(
        "/api/v1/repos/llm-context-vc/status",
        headers={"Authorization": f"Bearer {dave['access_token']}"},
    )
    assert r.status_code == 404


async def test_refresh_rotation_and_reuse_detection(client):
    tok = await oauth_login(client, "erin@example.com")
    r1 = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": tok["client_id"],
        },
    )
    assert r1.status_code == 200, r1.text
    new = r1.json()
    # Reusing the rotated token fails and revokes the family.
    r2 = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": tok["client_id"],
        },
    )
    assert r2.status_code == 400
    r3 = await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": new["refresh_token"],
            "client_id": tok["client_id"],
        },
    )
    assert r3.status_code == 400
    # Theft is assumed, so the family's access tokens are revoked too.
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 401


async def test_consent_deny_returns_error_to_client(client):
    reg = (
        await client.post(
            "/register",
            json={
                "client_name": "Some app",
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
            },
        )
    ).json()
    r = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "state": "s",
        },
    )
    req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
    await client.post("/auth/dev", data={"req": req, "email": "frank@example.com"})
    page = await client.get(f"/consent?req={req}")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    r = await client.post("/consent", data={"req": req, "csrf": csrf, "decision": "deny"})
    assert "error=access_denied" in r.headers["location"]
    client.cookies.clear()
