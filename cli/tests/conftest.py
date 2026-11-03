"""Runs a real ctx server (uvicorn subprocess, fake AI providers) against Postgres in Docker,
and logs a test user in through the dev sign-in so CLI commands hit real HTTP."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url():
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        port = _free_port()
        url = f"http://localhost:{port}"
        env = {
            **os.environ,
            "CTX_ENV": "dev",
            "CTX_DATABASE_URL": pg.get_connection_url(),
            "CTX_EMBEDDING_PROVIDER": "fake",
            "CTX_ARBITER_PROVIDER": "fake",
            "CTX_TASKS_PROVIDER": "inline",
            "CTX_STORAGE_PROVIDER": "db",
            "CTX_DEV_LOGIN": "true",
            "CTX_PUBLIC_BASE_URL": url,
            "CTX_RUN_MIGRATIONS_ON_START": "true",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ctx_server.app:app",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            env=env,
        )
        try:
            for _ in range(120):
                try:
                    if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("server did not start")
            yield url
        finally:
            proc.terminate()
            proc.wait(10)


def dev_token(server: str, email: str) -> dict:
    redirect = "http://127.0.0.1:1/callback"
    with httpx.Client(base_url=server, follow_redirects=False) as c:
        cid = c.post(
            "/register",
            json={
                "client_name": "ctx CLI",
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
            },
        ).json()["client_id"]
        ver = secrets.token_urlsafe(48)
        ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": redirect,
                "code_challenge": ch,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        c.post("/auth/dev", data={"req": req, "email": email})
        page = c.get(f"/consent?req={req}")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = c.post("/consent", data={"req": req, "csrf": csrf, "decision": "approve"})
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "client_id": cid,
                "code_verifier": ver,
            },
        ).json()
        return {**tok, "client_id": cid}


@pytest.fixture
def cli_env(server_url, tmp_path: Path, monkeypatch):
    from ctx_cli.config import Config

    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("CTX_CONFIG", str(cfg_path))
    tok = dev_token(server_url, f"cli-{secrets.token_hex(4)}@example.com")
    Config(
        server=server_url,
        client_id=tok["client_id"],
        access_token=tok["access_token"],
        refresh_token=tok["refresh_token"],
    ).save()
    return cfg_path
