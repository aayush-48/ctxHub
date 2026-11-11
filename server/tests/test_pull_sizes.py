"""Pull sizes: minimum (constraints and pinned items), recommended (every item, shortened where
possible) and full; items are shortened before any is dropped."""

from __future__ import annotations

import pytest
from ctx_server import service
from ctx_server.errors import ValidationFailed
from ctx_server.schemas import CommitIn, PullIn


def item(type_, subject, content, **kw):
    return {"type": type_, "subject": subject, "content": content, **kw}


async def seed(actor, repo):
    items = [item("constraint", "Hosting", "Must run on VW's existing SoCs; no extra hardware.")]
    items += [
        item(
            "decision",
            f"Choice {n}",
            f"Use option {n} for subsystem {n}.",
            reason=f"Option {n} is cheaper, faster and already deployed across the fleet in "
            f"region {n}, which keeps the rollout simple.",
        )
        for n in range(8)
    ]
    items += [
        item("fact", f"Fact {n}", f"Subsystem {n} handles part {n} of the hazard pipeline.")
        for n in range(8)
    ]
    await service.commit(actor, CommitIn(repo=repo, message="seed", items=items))


async def test_sizes_are_ordered_and_mean_what_they_say(alice, repo):
    await seed(alice, repo)
    r = await service.pull(alice, repo, PullIn(model="claude"))
    sizes = r["manifest"]["sizes"]
    assert sizes["minimum"] < sizes["recommended"] < sizes["full"]
    # No budget: the recommended size, with every item and nothing dropped.
    assert r["manifest"]["budget"] == sizes["recommended"]
    assert r["manifest"]["dropped"] == [] and r["manifest"]["included"] == 17
    assert r["manifest"]["tokens"] <= sizes["recommended"]

    full = (await service.pull(alice, repo, PullIn(budget=sizes["full"])))["manifest"]
    assert full["compressed"] == [] and full["dropped"] == []

    minimum = await service.pull(alice, repo, PullIn(budget=sizes["minimum"]))
    assert "Must run on VW" in minimum["context"]
    with pytest.raises(ValidationFailed) as e:
        await service.pull(alice, repo, PullIn(budget=max(50, sizes["minimum"] - 30)))
    assert e.value.details["minimum_budget"] == sizes["minimum"]


async def test_items_are_shortened_before_any_is_dropped(alice, repo):
    await seed(alice, repo)
    sizes = (await service.pull(alice, repo, PullIn()))["manifest"]["sizes"]
    between = (sizes["recommended"] + sizes["full"]) // 2
    m = (await service.pull(alice, repo, PullIn(budget=between)))["manifest"]
    assert m["dropped"] == [] and m["compressed"], "between the marks: some shortened, none lost"
    below = (await service.pull(alice, repo, PullIn(budget=sizes["recommended"] - 40)))["manifest"]
    # Below the recommended size something must go; whatever is kept fits the budget.
    assert below["dropped"] and below["tokens"] <= below["budget"]


async def test_previews_do_not_count_as_use(alice, repo):
    from ctx_server.db import session_scope
    from sqlalchemy import text

    await seed(alice, repo)

    async def uses() -> int:
        async with session_scope() as s:
            q = "SELECT coalesce(sum(use_count), 0) FROM state_items WHERE repo_id = :r"
            return (await s.execute(text(q), {"r": repo})).scalar_one()

    start = await uses()
    for _ in range(3):
        await service.pull(alice, repo, PullIn(), record_use=False)
    assert await uses() == start
    await service.pull(alice, repo, PullIn())  # a real load counts
    assert await uses() > start


async def test_short_forms_keep_what_was_replaced_and_why(alice, repo):
    await service.commit(
        alice,
        CommitIn(
            repo=repo,
            message="m",
            items=[
                item(
                    "fact",
                    "Database",
                    "The project stores everything in Firebase "
                    "Firestore, including bookings, doctors and clinic settings.",
                )
            ],
        ),
    )
    r = await service.commit(
        alice,
        CommitIn(
            repo=repo,
            message="m",
            items=[
                item(
                    "fact",
                    "Database",
                    "The project stores everything in Supabase "
                    "Postgres in Mumbai, including bookings, doctors and clinic settings.",
                    reason="Firestore costs were unpredictable",
                )
            ],
        ),
    )
    assert [e["action"] for e in r["entries"]] in (["supersede"], ["flag"])
    if r["entries"][0]["action"] == "flag":
        import uuid as _uuid

        from ctx_server.schemas import ConfirmIn

        await service.confirm(
            alice, _uuid.UUID(r["proposal_id"]), ConfirmIn(choices={"e1": "supersede"})
        )
    sizes = (await service.pull(alice, repo, PullIn()))["manifest"]["sizes"]
    tight = await service.pull(alice, repo, PullIn(budget=sizes["recommended"]))
    if tight["manifest"]["compressed"]:
        assert "(was: " in tight["context"]
    full = (await service.pull(alice, repo, PullIn(budget=sizes["full"])))["context"]
    assert "Firestore" in full and "unpredictable" in full
