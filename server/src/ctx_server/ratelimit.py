"""Per-user fixed-window rate limits, stored in Postgres so they hold across instances."""

from __future__ import annotations

import random
import uuid

from sqlalchemy import text

from .config import get_settings
from .db import session_scope
from .errors import RateLimited

HEAVY = "heavy"  # commit, import, pull, merge: run models or large renders
LIGHT = "light"


async def check(user_id: uuid.UUID, bucket: str = LIGHT) -> None:
    s = get_settings()
    limit = s.rate_limit_heavy_per_min if bucket == HEAVY else s.rate_limit_per_min
    async with session_scope() as db:
        count = (
            await db.execute(
                text(
                    "INSERT INTO rate_limits (key, window_start, count) "
                    "VALUES (:k, date_trunc('minute', now()), 1) "
                    "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limits.count + 1 "
                    "RETURNING count"
                ),
                {"k": f"{user_id}:{bucket}"},
            )
        ).scalar_one()
        if random.random() < 0.01:
            await db.execute(
                text("DELETE FROM rate_limits WHERE window_start < now() - interval '10 minutes'")
            )
    if count > limit:
        raise RateLimited(
            f"rate limit exceeded ({limit} {bucket} requests per minute); retry in a minute"
        )
