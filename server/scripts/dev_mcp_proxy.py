"""Development only: expose a local ctx server's /mcp without OAuth, for MCP Apps test hosts
(such as ext-apps' basic-host) that cannot sign in.

It signs in once through the development login (the server must run with CTX_DEV_LOGIN=true)
and forwards every request to the server with that user's bearer token.

    python server/scripts/dev_mcp_proxy.py --server http://localhost:8090 --port 3001 \
        --email you@example.com
    # then point the test host at http://localhost:3001/mcp
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding", "authorization"}


def dev_token(server: str, email: str) -> str:
    redirect = "http://127.0.0.1:1/callback"
    with httpx.Client(base_url=server, follow_redirects=False, timeout=30) as c:
        client_id = c.post(
            "/register",
            json={
                "client_name": "dev MCP proxy",
                "redirect_uris": [redirect],
                "token_endpoint_auth_method": "none",
            },
        ).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        r = c.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
            },
        )
        req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
        if c.post("/auth/dev", data={"req": req, "email": email}).status_code != 303:
            raise SystemExit("development login refused; start the server with CTX_DEV_LOGIN=true")
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
                "client_id": client_id,
                "code_verifier": verifier,
            },
        ).json()
        return tok["access_token"]


def build(server: str, token: str) -> Starlette:
    upstream = httpx.AsyncClient(base_url=server, timeout=120)

    async def forward(request: Request) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["authorization"] = f"Bearer {token}"
        r = await upstream.request(
            request.method,
            request.url.path,
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        out = {
            k: v for k, v in r.headers.items() if k.lower() not in HOP_BY_HOP | {"content-encoding"}
        }
        return Response(r.content, status_code=r.status_code, headers=out)

    return Starlette(
        routes=[Route("/mcp", forward, methods=["GET", "POST", "DELETE"])],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
                expose_headers=["*"],
            )
        ],
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--server", default="http://localhost:8090")
    ap.add_argument("--port", type=int, default=3001)
    ap.add_argument("--email", default="dev@example.com")
    args = ap.parse_args()
    if urlparse(args.server).hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit("this proxy is for local development servers only")
    token = dev_token(args.server, args.email)
    print(
        f"signed in as {args.email}; proxying http://localhost:{args.port}/mcp -> {args.server}/mcp"
    )
    uvicorn.run(build(args.server, token), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
