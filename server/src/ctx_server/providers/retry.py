"""Retries for calls to external model APIs, which fail transiently (dropped connections,
rate limits, 5xx)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


async def with_retries[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    what: str = "model call",
) -> T:
    """Run fn, retrying with exponential backoff and jitter. Re-raises the last error."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as e:  # network errors, 429 and 5xx from the provider SDKs
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1) * (0.5 + random.random())
            log.warning(
                "%s failed (%s: %s); retry %d/%d in %.1fs",
                what,
                type(e).__name__,
                e,
                attempt,
                attempts - 1,
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
