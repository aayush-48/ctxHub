"""HTTP client for the ctx REST API, with transparent token refresh."""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config


class ApiError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        msg = body.get("message") if isinstance(body, dict) else str(body)
        super().__init__(msg or f"HTTP {status}")

    @property
    def details(self) -> Any:
        return self.body.get("details") if isinstance(self.body, dict) else None


class NotLoggedIn(Exception):
    pass


class Client:
    def __init__(self, cfg: Config, timeout: float = 120):
        self.cfg = cfg
        self.http = httpx.Client(base_url=cfg.server.rstrip("/"), timeout=timeout)

    def _refresh(self) -> bool:
        if not (self.cfg.refresh_token and self.cfg.client_id):
            return False
        r = self.http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.cfg.refresh_token,
                "client_id": self.cfg.client_id,
            },
        )
        if r.status_code != 200:
            return False
        tok = r.json()
        self.cfg.access_token = tok["access_token"]
        self.cfg.refresh_token = tok.get("refresh_token", self.cfg.refresh_token)
        self.cfg.save()
        return True

    def request(self, method: str, path: str, **kw: Any) -> Any:
        if not self.cfg.access_token:
            raise NotLoggedIn("not logged in; run `ctx login`")
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.cfg.access_token}"}
            r = self.http.request(method, f"/api/v1{path}", headers=headers, **kw)
            if r.status_code == 401 and attempt == 0 and self._refresh():
                continue
            break
        if r.status_code == 401:
            raise NotLoggedIn("session expired; run `ctx login`")
        if r.status_code == 204:
            return None
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400:
            raise ApiError(r.status_code, body)
        return body

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=json, **kw)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)
