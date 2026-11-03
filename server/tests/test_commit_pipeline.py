"""Commit path: validation, pipeline decisions, confirm, and history."""

from __future__ import annotations

import uuid

import pytest
from ctx_server import service
from ctx_server.errors import Conflict, Forbidden, NotFound, ValidationFailed
from ctx_server.schemas import CommitIn, ConfirmIn, MemberIn
from pydantic import ValidationError


def item(type_, subject, content, **kw):
    return {"type": type_, "subject": subject, "content": content, **kw}


async def commit(actor, repo, items, updates=(), **kw):
    return await service.commit(
        actor,
        CommitIn(
            repo=repo, message=kw.pop("message", "m"), items=items, updates=list(updates), **kw
        ),
    )


async def test_init_repo_has_root_and_main(alice, repo):
    st = await service.status(alice, repo)
    assert st["branch"] == "main"
    assert st["active_total"] == 0
    log = await service.log(alice, repo, None)
    assert [c["kind"] for c in log["commits"]] == ["root"]


def test_malformed_payload_gives_field_errors():
    with pytest.raises(ValidationError) as e:
        CommitIn(repo="x", message="m", items=[{"type": "opinion", "subject": "", "content": "c"}])
    fields = {".".join(map(str, err["loc"])) for err in e.value.errors()}
    assert "items.0.type" in fields and "items.0.subject" in fields
    with pytest.raises(ValidationError):
        CommitIn(repo="x", message="m", items=[])


async def test_additive_commit_auto_confirms(alice, repo):
    r = await commit(
        alice,
        repo,
        [
            item("constraint", "Hosting", "Must run on GCP; no paid vector DB."),
            item(
                "decision",
                "Primary datastore",
                "Use Cloud SQL Postgres with pgvector.",
                reason="One database for state and vectors",
            ),
            item(
                "open_question", "ChatGPT write tools", "Does ChatGPT allow write tools over MCP?"
            ),
        ],
    )
    assert r["committed"] and r["additive_only"]
    assert r["counts"]["added"] == 3
    st = await service.status(alice, repo)
    assert st["active_items"] == {"constraint": 1, "decision": 1, "open_question": 1}


async def test_exact_duplicate_dropped_and_provenance_merged(alice, repo):
    await commit(alice, repo, [item("fact", "Region", "Cloud Run region is asia-south1.")])
    r = await commit(
        alice,
        repo,
        [item("fact", "Region", "Cloud Run region is asia-south1.")],
        source_client="chatgpt",
    )
    assert r["entries"][0]["action"] == "drop_duplicate"
    assert r["committed"] and r["counts"]["dropped"] == 1
    items = (await service.list_items(alice, repo, None))["items"]
    assert len(items) == 1


async def test_contradiction_supersedes_after_confirm(alice, repo):
    await commit(alice, repo, [item("decision", "Primary datastore", "Use a separate vector DB.")])
    r = await commit(
        alice, repo, [item("decision", "Primary datastore", "Use Postgres with pgvector.")]
    )
    assert not r["committed"]
    [e] = r["entries"]
    assert e["action"] == "supersede" and e["relation"] == "contradiction"
    # Nothing written before confirmation.
    assert (await service.status(alice, repo))["active_total"] == 1
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    assert c["counts"] == {"added": 1, "superseded": 1, "resolved": 0, "retracted": 0, "dropped": 0}
    all_items = (await service.list_items(alice, repo, None, include_inactive=True))["items"]
    old = next(i for i in all_items if "separate" in i["content"])
    new = next(i for i in all_items if "pgvector" in i["content"])
    assert old["status"] == "superseded" and old["superseded_by"] == new["id"]
    # History is kept: the old item is still active one commit back.
    prev = (await service.list_items(alice, repo, "main~1"))["items"]
    assert [i["content"] for i in prev] == ["Use a separate vector DB."]


async def test_refinement_keeps_item_key(alice, repo):
    await commit(alice, repo, [item("goal", "MVP", "Ship a CLI MVP")])
    r = await commit(alice, repo, [item("goal", "MVP", "Ship a CLI MVP before the next sprint")])
    [e] = r["entries"]
    assert e["action"] == "refine"
    await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    items = (await service.list_items(alice, repo, None, include_inactive=True))["items"]
    assert len({i["item_key"] for i in items}) == 1
    assert sorted(i["status"] for i in items) == ["active", "superseded"]


async def test_contradiction_across_types_supersedes(alice, repo):
    # The same thing recorded as a decision earlier and as a fact now must not stay twice.
    await commit(alice, repo, [item("decision", "Embedding size", "Embeddings have 768 dims.")])
    r = await commit(alice, repo, [item("fact", "Embedding size", "Embeddings have 1536 dims.")])
    [e] = r["entries"]
    assert e["action"] == "supersede"
    await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    items = (await service.list_items(alice, repo, None))["items"]
    assert [i["content"] for i in items] == ["Embeddings have 1536 dims."]


async def test_tasks_and_questions_are_not_compared_with_decisions(alice, repo):
    await commit(alice, repo, [item("decision", "Schema", "Use one table per entity.")])
    r = await commit(alice, repo, [item("task", "Schema", "Write the schema migration.")])
    assert r["entries"][0]["action"] == "add" and r["committed"]


async def test_temporal_scope_is_flagged_and_choice_applies(alice, repo):
    await commit(alice, repo, [item("decision", "Region", "Deploy to asia-south1.")])
    r = await commit(
        alice,
        repo,
        [item("decision", "Region", "Deploy to us-central1 for now while quotas are low.")],
    )
    [e] = r["entries"]
    assert e["action"] == "flag" and e["relation"] == "temporal_scope"
    await service.confirm(
        alice, uuid.UUID(r["proposal_id"]), ConfirmIn(choices={e["id"]: "keep_both"})
    )
    assert (await service.status(alice, repo))["active_items"]["decision"] == 2


async def test_partial_accept(alice, repo):
    await commit(alice, repo, [item("decision", "Datastore", "Use MySQL.")])
    r = await commit(
        alice,
        repo,
        [
            item("decision", "Datastore", "Use Postgres."),
            item("task", "Merge base", "Write the merge-base query."),
        ],
    )
    ids = {e["action"]: e["id"] for e in r["entries"]}
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn(accept=[ids["add"]]))
    assert c["counts"]["added"] == 1 and c["counts"]["superseded"] == 0


async def test_open_question_resolved_by_decision(alice, repo):
    await commit(
        alice,
        repo,
        [item("open_question", "ChatGPT write tools", "Does ChatGPT allow write tools over MCP?")],
    )
    r = await commit(
        alice,
        repo,
        [
            item(
                "decision", "ChatGPT write tools", "ChatGPT supports write tools in developer mode."
            )
        ],
    )
    actions = sorted(e["action"] for e in r["entries"])
    assert actions == ["add", "resolve"]
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    assert c["counts"]["resolved"] == 1


async def test_explicit_replace_and_status_updates(alice, repo):
    r1 = await commit(
        alice,
        repo,
        [
            item("task", "Schema", "Write the schema."),
            item("fact", "Region", "Region is asia-south1."),
        ],
    )
    items = (await service.list_items(alice, repo, None))["items"]
    task = next(i for i in items if i["type"] == "task")
    fact = next(i for i in items if i["type"] == "fact")
    r = await commit(
        alice,
        repo,
        [item("fact", "Deploy region", "Region is europe-west1.", replaces=fact["item_key"][:8])],
        updates=[{"item": task["id"][:8], "status": "resolved"}],
    )
    assert {e["action"] for e in r["entries"]} == {"refine", "status"}
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    assert c["counts"]["resolved"] == 1 and c["counts"]["superseded"] == 1
    assert r1["committed"]


async def test_bad_item_reference_is_a_field_error(alice, repo):
    with pytest.raises(ValidationFailed) as e:
        await commit(alice, repo, [], updates=[{"item": "deadbeef", "status": "retracted"}])
    assert e.value.details[0]["field"] == "updates.0.item"


async def test_stale_proposal_is_rejected(alice, repo):
    await commit(alice, repo, [item("decision", "Lang", "Use Go.")])
    r = await commit(alice, repo, [item("decision", "Lang", "Use Python.")])
    await commit(alice, repo, [item("task", "Other", "Something else entirely.")])
    with pytest.raises(Conflict):
        await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())


async def test_audit_log_records_every_stage(alice, repo):
    await commit(alice, repo, [item("decision", "DB", "Use MySQL.")])
    r = await commit(alice, repo, [item("decision", "DB", "Use Postgres.")])
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    shown = await service.show(alice, repo, c["commit"])
    stages = {a["stage"] for a in shown["audit"]}
    assert {"normalize", "embed", "classify", "supersede", "score", "compress", "confirm"} <= stages
    assert len(shown["diff"]["added"]) == 1 and len(shown["diff"]["superseded"]) == 1


async def test_revert_is_a_new_commit(alice, repo):
    await commit(alice, repo, [item("decision", "DB", "Use MySQL.")])
    r = await commit(alice, repo, [item("decision", "DB", "Use Postgres.")])
    c = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    rv = await service.revert(alice, repo, c["commit"])
    items = (await service.list_items(alice, repo, None))["items"]
    assert [i["content"] for i in items] == ["Use MySQL."]
    log = await service.log(alice, repo, None)
    assert log["commits"][0]["id"] == rv["commit"] and log["commits"][0]["kind"] == "revert"
    assert len(log["commits"]) == 4  # root, two commits, revert: nothing rewritten


async def test_roles_enforced(alice, bob, repo):
    with pytest.raises(NotFound):
        await service.status(bob, repo)
    await service.set_member(alice, repo, MemberIn(email=bob.email, role="viewer"))
    assert (await service.status(bob, repo))["role"] == "viewer"
    with pytest.raises(Forbidden):
        await commit(bob, repo, [item("task", "x", "y")])
    with pytest.raises(Forbidden):
        await service.set_member(bob, repo, MemberIn(email=bob.email, role="owner"))
    await service.set_member(alice, repo, MemberIn(email=bob.email, role="editor"))
    r = await commit(bob, repo, [item("task", "x", "y")])
    assert r["committed"]


async def test_repo_names_are_unique_across_owners(alice, bob):
    from ctx_server.schemas import RepoCreateIn

    name = f"Shared-{uuid.uuid4().hex[:6]}"
    await service.create_repo(alice, RepoCreateIn(name=name))
    with pytest.raises(Conflict, match="already taken"):
        await service.create_repo(bob, RepoCreateIn(name=name.lower()))
    # The owner can address it by name in any case; others still cannot see it.
    assert (await service.status(alice, name.upper()))["repo"] == name
    with pytest.raises(NotFound):
        await service.status(bob, name)
