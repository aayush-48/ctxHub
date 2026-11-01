"""Background jobs. Each job is a row in `jobs`; execution is triggered either in-process
(dev, tests, single instance) or by Cloud Tasks calling back into /internal/tasks/{id}, which
keeps work off the request path on Cloud Run."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from .config import get_settings
from .db import session_scope

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
_handlers: dict[str, Handler] = {}
_inflight: set[asyncio.Task] = set()
MAX_ATTEMPTS = 3


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn

    return deco


async def enqueue(kind: str, payload: dict[str, Any]) -> uuid.UUID:
    """Record a job and trigger it. Call after the transaction that created its inputs
    has committed."""
    job_id = uuid.uuid4()
    async with session_scope() as s:
        await s.execute(
            text("INSERT INTO jobs (id, kind, payload) VALUES (:id, :k, CAST(:p AS jsonb))"),
            {"id": job_id, "k": kind, "p": json.dumps(payload)},
        )
    settings = get_settings()
    if settings.tasks_provider == "cloud_tasks":
        await _create_cloud_task(job_id)
    else:
        t = asyncio.create_task(_run_inline(job_id))
        _inflight.add(t)
        t.add_done_callback(_inflight.discard)
    return job_id


async def _run_inline(job_id: uuid.UUID) -> None:
    for attempt in range(MAX_ATTEMPTS):
        try:
            await run_job(job_id)
            return
        except Exception:
            await asyncio.sleep(2**attempt)


async def drain() -> None:
    """Wait for in-process jobs (tests and graceful shutdown)."""
    while _inflight:
        await asyncio.gather(*list(_inflight), return_exceptions=True)


async def _create_cloud_task(job_id: uuid.UUID) -> None:
    from google.cloud import tasks_v2

    s = get_settings()
    if not s.tasks_queue or not s.tasks_service_account:
        raise RuntimeError("CTX_TASKS_QUEUE and CTX_TASKS_SERVICE_ACCOUNT are required")
    client = tasks_v2.CloudTasksAsyncClient()
    url = f"{s.public_base_url.rstrip('/')}/internal/tasks/{job_id}"
    await client.create_task(
        parent=s.tasks_queue,
        task=tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=url,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=s.tasks_service_account,
                    audience=s.public_base_url.rstrip("/"),
                ),
            ),
            dispatch_deadline={"seconds": 1800},
        ),
    )


async def run_job(job_id: uuid.UUID) -> str:
    """Claim and run a job. Returns its final status. Safe to call more than once."""
    async with session_scope() as s:
        row = (
            await s.execute(
                text(
                    "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                    "updated_at = now() WHERE id = :id AND status IN ('queued', 'retry') "
                    "RETURNING kind, payload, attempts"
                ),
                {"id": job_id},
            )
        ).first()
    if row is None:
        return "skipped"
    kind, payload, attempts = row
    try:
        await _handlers[kind](payload)
    except Exception as e:
        log.exception("job %s (%s) failed", job_id, kind)
        status = "failed" if attempts >= MAX_ATTEMPTS or not _retryable(e) else "retry"
        async with session_scope() as s:
            await s.execute(
                text("UPDATE jobs SET status = :st, error = :e, updated_at = now() WHERE id = :id"),
                {"st": status, "e": str(e)[:2000], "id": job_id},
            )
        if status == "retry":
            raise  # Cloud Tasks retries on a non-2xx response
        return status
    async with session_scope() as s:
        await s.execute(
            text(
                "UPDATE jobs SET status = 'done', error = NULL, updated_at = now() WHERE id = :id"
            ),
            {"id": job_id},
        )
    return "done"


def _retryable(e: Exception) -> bool:
    from .errors import CtxError, Unavailable

    return isinstance(e, Unavailable) or not isinstance(e, CtxError)
