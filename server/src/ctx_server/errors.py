"""Domain errors. REST maps them to HTTP status codes; MCP maps them to tool errors."""

from __future__ import annotations

from typing import Any


class CtxError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            d["details"] = self.details
        return d


class ValidationFailed(CtxError):
    status_code = 422
    code = "validation_failed"


class NotFound(CtxError):
    status_code = 404
    code = "not_found"


class Forbidden(CtxError):
    status_code = 403
    code = "forbidden"


class Conflict(CtxError):
    status_code = 409
    code = "conflict"


class RateLimited(CtxError):
    status_code = 429
    code = "rate_limited"


class Unavailable(CtxError):
    status_code = 503
    code = "unavailable"
