"""OAuth 2.1 authorization server state, backed by Postgres.

The MCP SDK provides the protocol endpoints (/authorize, /token, /register, /revoke, metadata,
PKCE checks); this provider stores clients, pending authorizations, codes and tokens. Users
authenticate with an upstream identity provider (Google or GitHub) in auth/routes.py.

Tokens are opaque random strings; only their SHA-256 is stored. Refresh tokens rotate, and
presenting an already-rotated refresh token revokes the whole token family.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import text

from ..config import get_settings
from ..db import session_scope

SCOPE = "ctx"
WEB_CLIENT_ID = "ctx-web"
PENDING_TTL_S = 600
CODE_TTL_S = 300


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class CtxAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider and TokenVerifier."""

    # -- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with session_scope() as s:
            info = (
                await s.execute(
                    text("SELECT info FROM oauth_clients WHERE client_id = :c"), {"c": client_id}
                )
            ).scalar_one_or_none()
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with session_scope() as s:
            await s.execute(
                text(
                    "INSERT INTO oauth_clients (client_id, info) VALUES (:c, CAST(:i AS jsonb)) "
                    "ON CONFLICT (client_id) DO UPDATE SET info = EXCLUDED.info"
                ),
                {"c": client_info.client_id, "i": client_info.model_dump_json(exclude_none=True)},
            )

    async def ensure_web_client(self) -> None:
        """The first-party web viewer: a public client using PKCE."""
        base = get_settings().public_base_url.rstrip("/")
        await self.register_client(
            OAuthClientInformationFull(
                client_id=WEB_CLIENT_ID,
                client_name="ctx web viewer",
                redirect_uris=[f"{base}/app/callback"],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=SCOPE,
            )
        )

    # -- authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        pending_id = secrets.token_urlsafe(24)
        async with session_scope() as s:
            await s.execute(text("DELETE FROM oauth_pending_auth WHERE expires_at < now()"))
            await s.execute(
                text(
                    "INSERT INTO oauth_pending_auth (id, client_id, params, expires_at) "
                    "VALUES (:id, :c, CAST(:p AS jsonb), :exp)"
                ),
                {
                    "id": pending_id,
                    "c": client.client_id,
                    "p": params.model_dump_json(),
                    "exp": _now() + timedelta(seconds=PENDING_TTL_S),
                },
            )
        base = get_settings().public_base_url.rstrip("/")
        return f"{base}/login?req={pending_id}"

    async def load_pending(
        self, pending_id: str
    ) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT client_id, params FROM oauth_pending_auth "
                        "WHERE id = :id AND expires_at > now()"
                    ),
                    {"id": pending_id},
                )
            ).first()
        if row is None:
            return None
        client = await self.get_client(row.client_id)
        if client is None:
            return None
        return client, AuthorizationParams.model_validate(row.params)

    async def issue_code(self, pending_id: str, user_id: uuid.UUID) -> str | None:
        """Consume a pending authorization for an authenticated user; returns the code."""
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "DELETE FROM oauth_pending_auth WHERE id = :id AND expires_at > now() "
                        "RETURNING client_id, params"
                    ),
                    {"id": pending_id},
                )
            ).first()
            if row is None:
                return None
            params = AuthorizationParams.model_validate(row.params)
            code = secrets.token_urlsafe(32)
            data = AuthorizationCode(
                code=code,
                scopes=params.scopes or [SCOPE],
                expires_at=time.time() + CODE_TTL_S,
                client_id=row.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=str(user_id),
            )
            await s.execute(
                text(
                    "INSERT INTO oauth_codes (code_hash, data, expires_at) "
                    "VALUES (:h, CAST(:d AS jsonb), :exp)"
                ),
                {
                    "h": _hash(code),
                    "d": data.model_dump_json(),
                    "exp": _now() + timedelta(seconds=CODE_TTL_S),
                },
            )
        return code

    async def deny(self, pending_id: str) -> None:
        async with session_scope() as s:
            await s.execute(
                text("DELETE FROM oauth_pending_auth WHERE id = :id"), {"id": pending_id}
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with session_scope() as s:
            data = (
                await s.execute(
                    text(
                        "SELECT data FROM oauth_codes WHERE code_hash = :h AND expires_at > now()"
                    ),
                    {"h": _hash(authorization_code)},
                )
            ).scalar_one_or_none()
        if data is None:
            return None
        code = AuthorizationCode.model_validate(data)
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with session_scope() as s:
            res = await s.execute(
                text("DELETE FROM oauth_codes WHERE code_hash = :h"),
                {"h": _hash(authorization_code.code)},
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                raise TokenError("invalid_grant", "authorization code already used")
            assert authorization_code.subject
            return await self._issue(
                s,
                client.client_id,
                uuid.UUID(authorization_code.subject),
                authorization_code.scopes,
                authorization_code.resource,
                uuid.uuid4(),
            )

    async def _issue(
        self,
        s,
        client_id: str,
        user_id: uuid.UUID,
        scopes: list[str],
        resource: str | None,
        family: uuid.UUID,
    ) -> OAuthToken:
        st = get_settings()
        access = "ctx_at_" + secrets.token_urlsafe(32)
        refresh = "ctx_rt_" + secrets.token_urlsafe(32)
        for tok, kind, ttl in (
            (access, "access", st.access_token_ttl_s),
            (refresh, "refresh", st.refresh_token_ttl_s),
        ):
            await s.execute(
                text(
                    "INSERT INTO oauth_tokens (token_hash, kind, family_id, client_id, user_id, "
                    "scopes, resource, expires_at) VALUES (:h, :k, :f, :c, :u, "
                    "CAST(:sc AS jsonb), :r, :exp)"
                ),
                {
                    "h": _hash(tok),
                    "k": kind,
                    "f": family,
                    "c": client_id,
                    "u": user_id,
                    "sc": json.dumps({"scopes": scopes}),
                    "r": resource,
                    "exp": _now() + timedelta(seconds=ttl),
                },
            )
        return OAuthToken(
            access_token=access,
            expires_in=st.access_token_ttl_s,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    # -- refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT family_id, client_id, user_id, scopes, resource, expires_at, revoked "
                        "FROM oauth_tokens WHERE token_hash = :h AND kind = 'refresh'"
                    ),
                    {"h": _hash(refresh_token)},
                )
            ).first()
            if row is None or row.client_id != client.client_id:
                return None
            if row.revoked:
                # Reuse of a rotated token: assume theft, revoke the family.
                await s.execute(
                    text("UPDATE oauth_tokens SET revoked = true WHERE family_id = :f"),
                    {"f": row.family_id},
                )
                return None
            if row.expires_at < _now():
                return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes["scopes"],
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "UPDATE oauth_tokens SET revoked = true WHERE token_hash = :h "
                        "AND revoked = false RETURNING family_id"
                    ),
                    {"h": _hash(refresh_token.token)},
                )
            ).first()
            if row is None:
                raise TokenError("invalid_grant", "refresh token already used")
            # Old access tokens of the family stay valid until they expire.
            assert refresh_token.subject
            return await self._issue(
                s,
                client.client_id,
                uuid.UUID(refresh_token.subject),
                scopes or refresh_token.scopes,
                refresh_token.resource,
                row.family_id,
            )

    # -- access tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with session_scope() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT client_id, user_id, scopes, resource, expires_at FROM oauth_tokens "
                        "WHERE token_hash = :h AND kind = 'access' AND revoked = false "
                        "AND expires_at > now()"
                    ),
                    {"h": _hash(token)},
                )
            ).first()
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes["scopes"],
            expires_at=int(row.expires_at.timestamp()),
            resource=row.resource,
            subject=str(row.user_id),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with session_scope() as s:
            await s.execute(
                text(
                    "UPDATE oauth_tokens SET revoked = true WHERE family_id = "
                    "(SELECT family_id FROM oauth_tokens WHERE token_hash = :h)"
                ),
                {"h": _hash(token.token)},
            )

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        async with session_scope() as s:
            await s.execute(
                text("UPDATE oauth_tokens SET revoked = true WHERE user_id = :u"), {"u": user_id}
            )


provider = CtxAuthProvider()
