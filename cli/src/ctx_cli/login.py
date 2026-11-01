"""Browser login: OAuth 2.1 authorization code + PKCE with a loopback redirect (RFC 8252).

The CLI registers itself as a public client (dynamic client registration) for the loopback
port it listens on, opens the browser, and receives the code on http://127.0.0.1:<port>.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

PAGE = (
    b"<!doctype html><meta charset=utf-8><title>ctx</title>"
    b"<body style='font:16px system-ui;padding:40px'>"
    b"<h2>%s</h2><p>You can close this tab and return to the terminal.</p>"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browser_login(server: str, open_browser: bool = True, timeout: float = 300) -> tuple[str, dict]:
    """Returns (client_id, token response)."""
    server = server.rstrip("/")
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/callback"
    with httpx.Client(base_url=server, timeout=30) as client:
        reg = client.post(
            "/register",
            json={
                "client_name": "ctx CLI",
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "ctx",
            },
        )
        reg.raise_for_status()
        client_id = reg.json()["client_id"]

        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(16)
        url = f"{server}/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "scope": "ctx",
            }
        )

        result: dict[str, str] = {}
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                if urlparse(self.path).path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in result and result.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(PAGE % (b"Signed in to ctx." if ok else b"Sign-in failed."))
                done.set()

            def log_message(self, *args) -> None:
                pass

        srv = HTTPServer(("127.0.0.1", port), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        print(f"Opening your browser to sign in. If it does not open, visit:\n{url}\n")
        if open_browser:
            webbrowser.open(url)
        try:
            if not done.wait(timeout):
                raise TimeoutError("timed out waiting for the browser sign-in")
        finally:
            srv.shutdown()
        if result.get("state") != state:
            raise RuntimeError("state mismatch; sign-in aborted")
        if "error" in result:
            raise RuntimeError(f"sign-in failed: {result['error']}")
        tok = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": redirect,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        tok.raise_for_status()
        return client_id, tok.json()


def revoke(server: str, client_id: str | None, token: str | None) -> None:
    if not (client_id and token):
        return
    try:
        httpx.post(
            f"{server.rstrip('/')}/revoke",
            data={"token": token, "client_id": client_id},
            timeout=10,
        )
    except httpx.HTTPError:
        pass
