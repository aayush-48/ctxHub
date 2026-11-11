"""Saving without a lookup: the model names stored items by subject, the server finds them,
and a change under a reworded subject is offered as "possibly replaces" for the user."""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from ctx_server import service
from ctx_server.errors import ValidationFailed
from ctx_server.schemas import CommitIn, ConfirmIn


def item(type_, subject, content, **kw):
    return {"type": type_, "subject": subject, "content": content, **kw}


async def commit(actor, repo, items, updates=(), auto=True):
    r = await service.commit(
        actor,
        CommitIn(repo=repo, message="m", items=items, updates=list(updates), auto_confirm=auto),
    )
    if not r["committed"] and auto:
        await service.confirm(actor, uuid.UUID(r["proposal_id"]), ConfirmIn())
    return r


async def active(actor, repo):
    return {
        i["subject"]: i["content"] for i in (await service.list_items(actor, repo, None))["items"]
    }


async def seed(actor, repo):
    await commit(
        actor,
        repo,
        [
            item("decision", "Primary database", "Use Firebase Firestore."),
            item("task", "PPO pipeline", "Build the PPO training pipeline."),
            item("open_question", "WhatsApp number", "One number per clinic or a shared one?"),
            item("fact", "Pilot clinics", "10 pilot clinics in Kothrud and Baner."),
        ],
    )


async def test_updates_by_subject(alice, repo):
    await seed(alice, repo)
    r = await commit(alice, repo, [], updates=[{"item": "PPO pipeline", "status": "resolved"}])
    [e] = r["entries"]
    assert e["action"] == "status" and e["target"]["subject"] == "PPO pipeline"
    assert "PPO pipeline" not in await active(alice, repo)


async def test_updates_by_a_few_words_of_the_subject_or_content(alice, repo):
    await seed(alice, repo)
    await commit(alice, repo, [], updates=[{"item": "training pipeline", "status": "resolved"}])
    assert "PPO pipeline" not in await active(alice, repo)


async def test_replaces_by_subject(alice, repo):
    await seed(alice, repo)
    r = await commit(
        alice,
        repo,
        [
            item(
                "decision", "Database choice", "Use Supabase Postgres.", replaces="Primary database"
            )
        ],
    )
    assert r["entries"][0]["relation"] == "explicit"
    now = await active(alice, repo)
    assert "Primary database" not in now and now["Database choice"] == "Use Supabase Postgres."


async def test_unknown_subject_fails_with_a_hint(alice, repo):
    await seed(alice, repo)
    with pytest.raises(ValidationFailed) as e:
        await commit(alice, repo, [], updates=[{"item": "Payment gateway", "status": "resolved"}])
    assert "no stored item clearly matches" in str(e.value.details)


async def test_reworded_subject_is_offered_as_possibly_replaces(alice, repo, runtime, monkeypatch):
    real = runtime.embedder.embed

    async def embed(texts):
        out = await real(texts)
        base = np.zeros(len(out[0]))
        base[0] = 1.0
        near = base.copy()
        near[1] = 0.48  # cosine ~0.90 with `base`: close, but not the same subject
        for i, t in enumerate(texts):
            if t == "Primary database":
                out[i] = base.tolist()
            elif t == "Persistence layer":
                out[i] = near.tolist()
        return out

    monkeypatch.setattr(runtime.embedder, "embed", embed)
    await seed(alice, repo)
    r = await commit(
        alice,
        repo,
        [item("decision", "Persistence layer", "Use Supabase Postgres.")],
        auto=False,
    )
    [e] = r["entries"]
    assert e["action"] == "flag" and e["relation"] == "possible_replace"
    assert e["target"]["subject"] == "Primary database" and e["default_choice"] == "keep_both"
    # The user says yes: the old item is replaced.
    await service.confirm(
        alice, uuid.UUID(r["proposal_id"]), ConfirmIn(choices={"e1": "supersede"})
    )
    now = await active(alice, repo)
    assert "Primary database" not in now and "Persistence layer" in now


async def test_search_returns_only_matching_items(alice, repo):
    await seed(alice, repo)
    r = await service.search_items(alice, repo, "database")
    assert [i["subject"] for i in r["items"]][:1] == ["Primary database"]
    assert r["total_active"] == 4 and len(r["items"]) < 4
