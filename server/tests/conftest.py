"""Test fixtures: a real Postgres + pgvector in Docker, migrated with the real migrations,
and deterministic fake embedding/arbiter providers."""

from __future__ import annotations

import asyncio
import os
import uuid

# Must be set before ctx_server modules read settings.
os.environ.update(
    CTX_ENV="test",
    CTX_EMBEDDING_PROVIDER="fake",
    CTX_ARBITER_PROVIDER="fake",
    CTX_TASKS_PROVIDER="inline",
    CTX_STORAGE_PROVIDER="db",
    CTX_RUN_MIGRATIONS_ON_START="false",
    CTX_DEV_LOGIN="true",
    CTX_PUBLIC_BASE_URL="http://localhost",
)

import pytest  # noqa: E402
from ctx_server.config import get_settings  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402


@pytest.fixture(scope="session")
def pg_url():
    external = os.environ.get("CTX_TEST_DATABASE_URL")
    if external:
        yield external
        return
    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
async def database(pg_url):
    os.environ["CTX_DATABASE_URL"] = pg_url
    get_settings.cache_clear()
    from ctx_server.db import dispose_engine, init_engine
    from ctx_server.migrate import upgrade_head
    from ctx_server.runtime import build_runtime, set_runtime

    await asyncio.to_thread(upgrade_head, pg_url)
    init_engine(pg_url)
    set_runtime(build_runtime(get_settings()))
    yield
    await dispose_engine()


@pytest.fixture
def runtime():
    from ctx_server.runtime import get_runtime

    return get_runtime()


async def make_actor(name: str = "user"):
    from ctx_server.service import Actor, upsert_user

    email = f"{name}-{uuid.uuid4().hex[:8]}@example.com"
    uid = await upsert_user(f"test:{email}", email, name)
    return Actor(id=uid, email=email)


@pytest.fixture
async def alice():
    return await make_actor("alice")


@pytest.fixture
async def bob():
    return await make_actor("bob")


@pytest.fixture
async def repo(alice):
    from ctx_server import service
    from ctx_server.schemas import RepoCreateIn

    r = await service.create_repo(alice, RepoCreateIn(name=f"r{uuid.uuid4().hex[:8]}"))
    return r["id"]
