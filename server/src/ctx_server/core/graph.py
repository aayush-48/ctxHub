"""Commit graph queries: ancestors, merge base, ref resolution, and writing commits."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import Conflict, NotFound, ValidationFailed

_ANCESTORS_SQL = text(
    """
    WITH RECURSIVE anc(id, depth) AS (
        SELECT CAST(:start AS uuid), 0
      UNION
        SELECT cp.parent_id, anc.depth + 1
        FROM commit_parents cp JOIN anc ON cp.commit_id = anc.id
        WHERE anc.depth < :max_depth
    )
    SELECT id, min(depth) AS depth FROM anc GROUP BY id
    """
)


async def ancestors(
    session: AsyncSession, commit_id: uuid.UUID, max_depth: int = 100_000
) -> dict[uuid.UUID, int]:
    """All ancestors of commit_id (inclusive) mapped to their minimum distance."""
    rows = await session.execute(_ANCESTORS_SQL, {"start": commit_id, "max_depth": max_depth})
    return {r.id: r.depth for r in rows}


async def merge_base(session: AsyncSession, a: uuid.UUID, b: uuid.UUID) -> uuid.UUID | None:
    """Lowest common ancestor of a and b.

    Among common ancestors, keep those that are not an ancestor of another common ancestor.
    With criss-cross histories more than one remains; pick the newest for determinism.
    """
    anc_a = await ancestors(session, a)
    anc_b = await ancestors(session, b)
    common = set(anc_a) & set(anc_b)
    if not common:
        return None
    if len(common) == 1:
        return next(iter(common))
    # c is "not lowest" if some other common ancestor d has c as an ancestor.
    rows = await session.execute(
        text(
            "SELECT commit_id, parent_id FROM commit_parents "
            "WHERE commit_id = ANY(:ids) AND parent_id = ANY(:ids)"
        ),
        {"ids": list(common)},
    )
    has_common_child = {r.parent_id for r in rows}
    lowest = [c for c in common if c not in has_common_child]
    if len(lowest) == 1:
        return lowest[0]
    created = await session.execute(
        text("SELECT id FROM commits WHERE id = ANY(:ids) ORDER BY created_at DESC LIMIT 1"),
        {"ids": lowest},
    )
    return created.scalar_one()


async def parents(session: AsyncSession, commit_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        text("SELECT parent_id FROM commit_parents WHERE commit_id = :c ORDER BY ord"),
        {"c": commit_id},
    )
    return [r.parent_id for r in rows]


_REF = re.compile(r"^(?P<base>[^~^]+)(?P<suffix>(?:[~^]\d*)*)$")


async def resolve_ref(session: AsyncSession, repo_id: uuid.UUID, ref: str) -> uuid.UUID:
    """Resolve a branch name, full commit id, unique id prefix (>= 4 hex chars), optionally
    followed by ~N (Nth first-parent ancestor) or ^N (Nth parent)."""
    m = _REF.match(ref.strip())
    if not m:
        raise ValidationFailed(f"invalid ref: {ref!r}")
    base, suffix = m.group("base"), m.group("suffix")
    commit_id = await _resolve_base(session, repo_id, base)
    for op, num in re.findall(r"([~^])(\d*)", suffix):
        n = int(num) if num else 1
        if op == "~":
            for _ in range(n):
                ps = await parents(session, commit_id)
                if not ps:
                    raise NotFound(f"{ref}: walks past the root commit")
                commit_id = ps[0]
        else:
            ps = await parents(session, commit_id)
            if n < 1 or n > len(ps):
                raise NotFound(f"{ref}: commit has no parent {n}")
            commit_id = ps[n - 1]
    return commit_id


async def _resolve_base(session: AsyncSession, repo_id: uuid.UUID, base: str) -> uuid.UUID:
    row = await session.execute(
        text("SELECT head_commit_id FROM branches WHERE repo_id = :r AND name = :n"),
        {"r": repo_id, "n": base},
    )
    head = row.scalar_one_or_none()
    if head is not None:
        return head
    hexpart = base.replace("-", "").lower()
    if re.fullmatch(r"[0-9a-f]{4,32}", hexpart):
        rows = await session.execute(
            text(
                "SELECT id FROM commits WHERE repo_id = :r "
                "AND replace(id::text, '-', '') LIKE :p LIMIT 2"
            ),
            {"r": repo_id, "p": hexpart + "%"},
        )
        ids = [r.id for r in rows]
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            raise ValidationFailed(f"ambiguous commit prefix {base!r}")
    raise NotFound(f"unknown ref {base!r}")


async def insert_commit(
    session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    author_id: uuid.UUID,
    message: str,
    kind: str,
    parent_ids: list[uuid.UUID],
    source_client: str | None = None,
    source_model: str | None = None,
    commit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Write a commit row and its parents. Membership is written separately, after any new
    items (which reference the commit through created_in) are inserted."""
    cid = commit_id or uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO commits (id, repo_id, author_id, message, kind, source_client, "
            "source_model) VALUES (:id, :r, :a, :m, :k, :sc, :sm)"
        ),
        {
            "id": cid,
            "r": repo_id,
            "a": author_id,
            "m": message,
            "k": kind,
            "sc": source_client,
            "sm": source_model,
        },
    )
    for ord_, p in enumerate(parent_ids):
        await session.execute(
            text("INSERT INTO commit_parents (commit_id, parent_id, ord) VALUES (:c, :p, :o)"),
            {"c": cid, "p": p, "o": ord_},
        )
    return cid


async def write_membership(
    session: AsyncSession,
    commit_id: uuid.UUID,
    membership: dict[uuid.UUID, tuple[str, uuid.UUID | None]],
) -> None:
    if not membership:
        return
    await session.execute(
        text(
            "INSERT INTO commit_items (commit_id, item_id, status, superseded_by) "
            "SELECT :c, x.item_id, x.status, x.sup FROM unnest("
            "CAST(:ids AS uuid[]), CAST(:sts AS text[]), CAST(:sups AS uuid[])"
            ") AS x(item_id, status, sup)"
        ),
        {
            "c": commit_id,
            "ids": list(membership.keys()),
            "sts": [s for s, _ in membership.values()],
            "sups": [sup for _, sup in membership.values()],
        },
    )


async def move_branch(
    session: AsyncSession,
    repo_id: uuid.UUID,
    branch: str,
    new_head: uuid.UUID,
    expected_head: uuid.UUID | None,
) -> None:
    """Compare-and-swap the branch head; fails if someone else moved it first."""
    if expected_head is None:
        res = await session.execute(
            text("UPDATE branches SET head_commit_id = :n WHERE repo_id = :r AND name = :b"),
            {"n": new_head, "r": repo_id, "b": branch},
        )
    else:
        res = await session.execute(
            text(
                "UPDATE branches SET head_commit_id = :n "
                "WHERE repo_id = :r AND name = :b AND head_commit_id = :e"
            ),
            {"n": new_head, "r": repo_id, "b": branch, "e": expected_head},
        )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        raise Conflict(
            f"branch {branch!r} moved while this operation was in progress; retry against "
            "the new head"
        )


def ids(xs: Iterable[uuid.UUID]) -> list[str]:
    return [str(x) for x in xs]
