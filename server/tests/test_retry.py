"""Transient failures of model APIs (here: embeddings) are retried with backoff."""

from __future__ import annotations

import pytest
from ctx_server.providers import retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(retry.asyncio, "sleep", instant)


def flaky(failures: int):
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] <= failures:
            raise ConnectionError("connection reset")
        return "ok"

    return fn, calls


async def test_transient_failure_is_retried():
    fn, calls = flaky(2)
    assert await retry.with_retries(fn) == "ok"
    assert calls["n"] == 3


async def test_persistent_failure_is_raised_after_retries():
    fn, calls = flaky(10)
    with pytest.raises(ConnectionError):
        await retry.with_retries(fn, attempts=3)
    assert calls["n"] == 3
