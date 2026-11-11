"""Pulls on large repos: where-you-left-off by default, a focus area on request, everything
only when a size is asked for. Small repos load whole, as before."""

from __future__ import annotations

from ctx_server import service
from ctx_server.schemas import CommitIn, PullIn

SERVICES = ["Amber Falcon", "Jade Harbor", "Onyx Meadow", "Coral Summit"]
ASPECTS = ["database", "region", "owner", "cache", "queue", "monitoring", "language", "search"]


def item(type_, subject, content):
    return {"type": type_, "subject": subject, "content": content}


async def seed_large(actor, repo):
    items = [item("constraint", "Budget", "First-year budget is Rs 3 lakh.")]
    items += [
        item("fact", f"{s} {a}", f"The {s} service's {a} is option {n} of the platform plan.")
        for s in SERVICES
        for n, a in enumerate(ASPECTS)
    ]
    for i in range(0, len(items), 50):
        await service.commit(actor, CommitIn(repo=repo, message="seed", items=items[i : i + 50]))
    # The latest change, made after everything else.
    await service.commit(
        actor,
        CommitIn(
            repo=repo,
            message="latest",
            items=[item("decision", "Payment gateway", "Switch payments to Cashfree.")],
        ),
    )


def small_threshold(runtime, monkeypatch):
    monkeypatch.setattr(runtime.settings, "large_repo_tokens", 300)
    monkeypatch.setattr(runtime.settings, "focus_pull_budget", 600)
    monkeypatch.setattr(runtime.settings, "pull_recent_items", 3)


async def test_small_repo_loads_everything(alice, repo):
    await service.commit(
        alice, CommitIn(repo=repo, message="m", items=[item("fact", "Region", "Runs in Mumbai.")])
    )
    m = (await service.pull(alice, repo, PullIn()))["manifest"]
    assert m["mode"] == "all" and m["not_loaded"] == 0 and m["large"] is False


async def test_large_repo_loads_where_you_left_off(alice, repo, runtime, monkeypatch):
    await seed_large(alice, repo)
    small_threshold(runtime, monkeypatch)
    r = await service.pull(alice, repo, PullIn())
    m = r["manifest"]
    assert m["large"] is True and m["mode"] == "starter"
    assert "Budget" in r["context"]  # what always matters
    assert "Payment gateway" in r["context"]  # the latest change
    assert m["not_loaded"] > 20 and "Also stored but not loaded" in r["context"]
    assert {a["label"] for a in m["areas"]} >= {"Amber Falcon", "Jade Harbor"}
    assert m["tokens"] < m["sizes"]["recommended"]


async def test_focus_loads_the_matching_area(alice, repo, runtime, monkeypatch):
    await seed_large(alice, repo)
    small_threshold(runtime, monkeypatch)
    r = await service.pull(alice, repo, PullIn(focus="Onyx Meadow"))
    m = r["manifest"]
    assert m["mode"] == "focus" and m["focus"] == "Onyx Meadow"
    assert r["context"].count("Onyx Meadow") >= 6
    assert "Coral Summit database" not in r["context"]


async def test_a_size_asked_for_loads_everything_that_fits(alice, repo, runtime, monkeypatch):
    await seed_large(alice, repo)
    small_threshold(runtime, monkeypatch)
    sizes = (await service.pull(alice, repo, PullIn()))["manifest"]["sizes"]
    m = (await service.pull(alice, repo, PullIn(budget=sizes["recommended"])))["manifest"]
    assert m["mode"] == "all" and m["not_loaded"] == 0 and m["dropped"] == []
