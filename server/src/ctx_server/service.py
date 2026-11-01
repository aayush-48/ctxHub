"""Service layer: every REST endpoint and MCP tool goes through these functions.

Authorization is enforced in exactly one place, `_authorize`, which every repo-scoped
operation calls with the minimum role it needs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import tasks
from .core import graph
from .core.imports import extract_items, parse_export, select_conversation
from .core.items import NewItem, insert_item
from .core.merge import (
    membership_from_json,
    plan_merge,
    plan_to_json,
    state_from_json,
)
from .core.pipeline import apply_proposal, public_entries, run_pipeline
from .core.render import render
from .core.snapshot import Snapshot, load_snapshot
from .core.text import heuristic_compress, normalize_subject, normalize_text
from .db import session_scope
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .runtime import get_runtime
from .schemas import (
    BranchCreateIn,
    CommitIn,
    ConfirmIn,
    MemberIn,
    PullIn,
    RepoCreateIn,
    RepoSettingsIn,
    ResolveIn,
)
from .telemetry import span

ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}
DEFAULT_REPO_SETTINGS = {
    "auto_commit_additive": True,
    "auto_merge_additive": True,
    "llm_enabled": True,
}


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    email: str | None = None
    client_id: str | None = None


@dataclass
class RepoCtx:
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    role: str
    settings: dict[str, Any]


# ---------------------------------------------------------------------------- users


async def upsert_user(idp_subject: str, email: str | None, display_name: str | None) -> uuid.UUID:
    async with session_scope() as s:
        row = await s.execute(
            text(
                "INSERT INTO users (id, idp_subject, email, display_name) "
                "VALUES (:id, :sub, :email, :name) "
                "ON CONFLICT (idp_subject) DO UPDATE SET email = EXCLUDED.email, "
                "display_name = COALESCE(EXCLUDED.display_name, users.display_name) "
                "RETURNING id"
            ),
            {"id": uuid.uuid4(), "sub": idp_subject, "email": email, "name": display_name},
        )
        return row.scalar_one()


async def get_user(user_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        r = (
            (
                await s.execute(
                    text("SELECT id, email, display_name, created_at FROM users WHERE id = :id"),
                    {"id": user_id},
                )
            )
            .mappings()
            .first()
        )
    if r is None:
        raise NotFound("user not found")
    return {"id": str(r["id"]), "email": r["email"], "display_name": r["display_name"]}


# ---------------------------------------------------------------------------- authz


async def _authorize(s: AsyncSession, actor: Actor, repo_ref: str, need: str) -> RepoCtx:
    """Resolve a repo reference the actor can see and check the actor's role on it.

    repo_ref is a repo id, a repo name the actor is a member of, or "<owner email>/<name>".
    Unknown and forbidden repos are reported identically (not found) unless the actor can at
    least view the repo, so repo names are not disclosed to non-members.
    """
    params: dict[str, Any] = {"u": actor.id}
    ref = repo_ref.strip()
    try:
        params["id"] = uuid.UUID(ref)
        where = "r.id = :id"
    except ValueError:
        if "/" in ref:
            owner, name = ref.rsplit("/", 1)
            params |= {"owner": owner.lower(), "name": name}
            where = "r.name = :name AND lower(o.email) = :owner"
        else:
            params["name"] = ref
            where = "r.name = :name"
    rows = (
        (
            await s.execute(
                text(
                    f"SELECT r.id, r.name, r.owner_id, r.settings, m.role, o.email AS owner_email "
                    f"FROM repos r JOIN repo_members m ON m.repo_id = r.id AND m.user_id = :u "
                    f"JOIN users o ON o.id = r.owner_id WHERE {where}"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise NotFound(f"repo {repo_ref!r} not found")
    if len(rows) > 1:
        owned = [r for r in rows if r["owner_id"] == actor.id]
        if len(owned) == 1:
            rows = owned
        else:
            raise ValidationFailed(
                f"repo name {repo_ref!r} is ambiguous; use <owner email>/<name> or the repo id",
                [{"id": str(r["id"]), "owner": r["owner_email"]} for r in rows],
            )
    r = rows[0]
    if ROLE_RANK[r["role"]] < ROLE_RANK[need]:
        raise Forbidden(f"this needs the {need} role on {r['name']!r}; you are {r['role']}")
    return RepoCtx(
        id=r["id"],
        name=r["name"],
        owner_id=r["owner_id"],
        role=r["role"],
        settings={**DEFAULT_REPO_SETTINGS, **(r["settings"] or {})},
    )


async def _authorize_by_id(s: AsyncSession, actor: Actor, repo_id: uuid.UUID, need: str) -> RepoCtx:
    return await _authorize(s, actor, str(repo_id), need)


# ---------------------------------------------------------------------------- repos


async def create_repo(actor: Actor, body: RepoCreateIn) -> dict[str, Any]:
    async with session_scope() as s:
        exists = await s.execute(
            text("SELECT 1 FROM repos WHERE owner_id = :u AND name = :n"),
            {"u": actor.id, "n": body.name},
        )
        if exists.first():
            raise Conflict(f"you already have a repo named {body.name!r}")
        repo_id = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO repos (id, owner_id, name, description, settings) "
                "VALUES (:id, :u, :n, :d, CAST(:st AS jsonb))"
            ),
            {
                "id": repo_id,
                "u": actor.id,
                "n": body.name,
                "d": body.description,
                "st": json.dumps(DEFAULT_REPO_SETTINGS),
            },
        )
        await s.execute(
            text("INSERT INTO repo_members (repo_id, user_id, role) VALUES (:r, :u, 'owner')"),
            {"r": repo_id, "u": actor.id},
        )
        root = await graph.insert_commit(
            s,
            repo_id=repo_id,
            author_id=actor.id,
            message="Initialize repository",
            kind="root",
            parent_ids=[],
            source_client="server",
        )
        await s.execute(
            text("INSERT INTO branches (repo_id, name, head_commit_id) VALUES (:r, 'main', :c)"),
            {"r": repo_id, "c": root},
        )
        await _set_head(s, actor.id, repo_id, "main", None)
    return {"id": str(repo_id), "name": body.name, "branch": "main", "root_commit": str(root)}


async def list_repos(actor: Actor) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT r.id, r.name, r.description, r.created_at, m.role, o.email AS owner "
                        "FROM repos r JOIN repo_members m ON m.repo_id = r.id AND m.user_id = :u "
                        "JOIN users o ON o.id = r.owner_id ORDER BY r.created_at DESC"
                    ),
                    {"u": actor.id},
                )
            )
            .mappings()
            .all()
        )
    return [{**_jsonable(dict(r))} for r in rows]


async def get_repo(actor: Actor, repo: str) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        branches = await _branches(s, rc.id)
        owner = (
            await s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": rc.owner_id})
        ).scalar_one()
    return {
        "id": str(rc.id),
        "name": rc.name,
        "owner": owner,
        "role": rc.role,
        "settings": rc.settings,
        "branches": branches,
    }


async def update_repo_settings(actor: Actor, repo: str, body: RepoSettingsIn) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "owner")
        new = {**rc.settings, **body.model_dump(exclude_none=True)}
        await s.execute(
            text("UPDATE repos SET settings = CAST(:st AS jsonb) WHERE id = :id"),
            {"st": json.dumps(new), "id": rc.id},
        )
    return new


async def delete_repo(actor: Actor, repo: str) -> None:
    """Hard delete: commits, items, audit rows, uploads and stored export files."""
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "owner")
        # Break FK cycles (branches -> commits, user_heads -> commits) via cascades on repo.
        await s.execute(text("DELETE FROM repos WHERE id = :id"), {"id": rc.id})
    await get_runtime().blob.delete_repo(rc.id)


# ---------------------------------------------------------------------------- members


async def list_members(actor: Actor, repo: str) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT u.id, u.email, u.display_name, m.role FROM repo_members m "
                        "JOIN users u ON u.id = m.user_id WHERE m.repo_id = :r ORDER BY u.email"
                    ),
                    {"r": rc.id},
                )
            )
            .mappings()
            .all()
        )
    return [_jsonable(dict(r)) for r in rows]


async def set_member(actor: Actor, repo: str, body: MemberIn) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "owner")
        uid = (
            await s.execute(
                text(
                    "SELECT id FROM users WHERE lower(email) = lower(:e) ORDER BY created_at LIMIT 1"
                ),
                {"e": body.email},
            )
        ).scalar_one_or_none()
        if uid is None:
            raise NotFound(f"{body.email} has not signed in yet; ask them to sign in once first")
        if uid == rc.owner_id and body.role != "owner":
            raise ValidationFailed("the repo creator always keeps the owner role")
        await s.execute(
            text(
                "INSERT INTO repo_members (repo_id, user_id, role) VALUES (:r, :u, :role) "
                "ON CONFLICT (repo_id, user_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"r": rc.id, "u": uid, "role": body.role},
        )
    return {"email": body.email, "role": body.role}


async def remove_member(actor: Actor, repo: str, email: str) -> None:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "owner")
        uid = (
            await s.execute(
                text("SELECT id FROM users WHERE lower(email) = lower(:e)"), {"e": email}
            )
        ).scalar_one_or_none()
        if uid is None:
            raise NotFound(f"no member {email}")
        if uid == rc.owner_id:
            raise ValidationFailed("cannot remove the repo creator")
        await s.execute(
            text("DELETE FROM repo_members WHERE repo_id = :r AND user_id = :u"),
            {"r": rc.id, "u": uid},
        )


# ---------------------------------------------------------------------------- refs, heads


async def _branches(s: AsyncSession, repo_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await s.execute(
                text(
                    "SELECT name, head_commit_id, fork_commit_id, created_at FROM branches "
                    "WHERE repo_id = :r ORDER BY name"
                ),
                {"r": repo_id},
            )
        )
        .mappings()
        .all()
    )
    return [_jsonable(dict(r)) for r in rows]


async def _set_head(
    s: AsyncSession,
    user_id: uuid.UUID,
    repo_id: uuid.UUID,
    branch: str | None,
    commit_id: uuid.UUID | None,
) -> None:
    await s.execute(
        text(
            "INSERT INTO user_heads (user_id, repo_id, branch, commit_id) "
            "VALUES (:u, :r, :b, :c) ON CONFLICT (user_id, repo_id) "
            "DO UPDATE SET branch = EXCLUDED.branch, commit_id = EXCLUDED.commit_id"
        ),
        {"u": user_id, "r": repo_id, "b": branch, "c": commit_id},
    )


async def _current(s: AsyncSession, actor: Actor, rc: RepoCtx) -> tuple[str | None, uuid.UUID]:
    """The actor's checkout: (branch or None if detached, commit id)."""
    row = (
        await s.execute(
            text("SELECT branch, commit_id FROM user_heads WHERE user_id = :u AND repo_id = :r"),
            {"u": actor.id, "r": rc.id},
        )
    ).first()
    branch = row.branch if row else "main"
    if branch:
        head = (
            await s.execute(
                text("SELECT head_commit_id FROM branches WHERE repo_id = :r AND name = :b"),
                {"r": rc.id, "b": branch},
            )
        ).scalar_one_or_none()
        if head is not None:
            return branch, head
        branch = "main"  # checked-out branch was deleted; fall back to main
        head = (
            await s.execute(
                text("SELECT head_commit_id FROM branches WHERE repo_id = :r AND name = 'main'"),
                {"r": rc.id},
            )
        ).scalar_one()
        return branch, head
    assert row is not None and row.commit_id is not None
    return None, row.commit_id


async def _branch_head(
    s: AsyncSession, repo_id: uuid.UUID, branch: str, for_update: bool = False
) -> uuid.UUID:
    row = await s.execute(
        text(
            "SELECT head_commit_id FROM branches WHERE repo_id = :r AND name = :b"
            + (" FOR UPDATE" if for_update else "")
        ),
        {"r": repo_id, "b": branch},
    )
    head = row.scalar_one_or_none()
    if head is None:
        raise NotFound(f"branch {branch!r} not found")
    return head


async def _write_branch(s: AsyncSession, actor: Actor, rc: RepoCtx, branch: str | None) -> str:
    """The branch a write goes to: explicit, else the actor's checkout. Detached HEAD refuses."""
    if branch:
        await _branch_head(s, rc.id, branch)
        return branch
    cur, _ = await _current(s, actor, rc)
    if cur is None:
        raise ValidationFailed(
            "you are on a detached commit; create a branch first "
            "(ctx branch <name>) or check out a branch"
        )
    return cur


async def _ref_commit(
    s: AsyncSession, actor: Actor, rc: RepoCtx, ref: str | None
) -> tuple[str | None, uuid.UUID]:
    """Resolve a ref, where HEAD (optionally HEAD~N / HEAD^N) is the actor's checkout."""
    if not ref or ref == "HEAD":
        return await _current(s, actor, rc)
    if ref.startswith(("HEAD~", "HEAD^")):
        _, head = await _current(s, actor, rc)
        return None, await graph.resolve_ref(s, rc.id, head.hex + ref[4:])
    commit = await graph.resolve_ref(s, rc.id, ref)
    is_branch = (
        await s.execute(
            text("SELECT 1 FROM branches WHERE repo_id = :r AND name = :n"),
            {"r": rc.id, "n": ref},
        )
    ).first()
    return (ref if is_branch else None), commit


# ---------------------------------------------------------------------------- status, items


async def status(actor: Actor, repo: str) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        branch, head = await _current(s, actor, rc)
        counts = (
            await s.execute(
                text(
                    "SELECT si.type, count(*) AS n FROM commit_items ci JOIN state_items si "
                    "ON si.id = ci.item_id WHERE ci.commit_id = :c AND ci.status = 'active' "
                    "GROUP BY si.type"
                ),
                {"c": head},
            )
        ).all()
        merges = (
            await s.execute(
                text(
                    "SELECT m.id, m.theirs_ref, count(c.id) FILTER (WHERE c.resolution IS NULL) "
                    "AS open FROM merges m LEFT JOIN merge_conflicts c ON c.merge_id = m.id "
                    "WHERE m.repo_id = :r AND m.ours_branch = :b AND m.state = 'pending' "
                    "GROUP BY m.id"
                ),
                {"r": rc.id, "b": branch},
            )
        ).all()
        proposals = (
            await s.execute(
                text(
                    "SELECT count(*) FROM proposals WHERE repo_id = :r AND author_id = :u "
                    "AND status = 'pending' AND expires_at > now()"
                ),
                {"r": rc.id, "u": actor.id},
            )
        ).scalar_one()
        commit = (
            await s.execute(
                text("SELECT message, created_at FROM commits WHERE id = :c"), {"c": head}
            )
        ).first()
    return {
        "repo": rc.name,
        "repo_id": str(rc.id),
        "role": rc.role,
        "branch": branch,
        "detached": branch is None,
        "head": str(head),
        "head_message": commit.message if commit else None,
        "active_items": {t: n for t, n in counts},
        "active_total": sum(n for _, n in counts),
        "pending_merge": [
            {"merge_id": str(m.id), "from": m.theirs_ref, "open_conflicts": m.open} for m in merges
        ],
        "pending_proposals": proposals,
    }


async def list_items(
    actor: Actor, repo: str, ref: str | None, include_inactive: bool = False
) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        branch, commit = await _ref_commit(s, actor, rc, ref)
        snap = await load_snapshot(s, commit)
    items = list(snap.items.values()) if include_inactive else snap.active()
    items.sort(key=lambda i: (i.type, -i.importance))
    return {"commit": str(commit), "branch": branch, "items": [i.view() for i in items]}


# ---------------------------------------------------------------------------- commit path


async def commit(actor: Actor, body: CommitIn) -> dict[str, Any]:
    rt = get_runtime()
    async with session_scope() as s:
        rc = await _authorize(s, actor, body.repo, "editor")
        branch = await _write_branch(s, actor, rc, body.branch)
        base_id = await _branch_head(s, rc.id, branch)
        base = await load_snapshot(s, base_id)
    provenance = {"client": body.source_client, "model": body.source_model, "author": str(actor.id)}
    with span(
        "ctx.pipeline",
        repo=str(rc.id),
        items=len(body.items),
        updates=len(body.updates),
        client=body.source_client,
    ):
        result = await run_pipeline(
            rt.pipeline(),
            base,
            body.items,
            body.updates,
            provenance,
            llm_enabled=bool(rc.settings.get("llm_enabled", True)),
        )
    auto = (
        body.auto_confirm
        if body.auto_confirm is not None
        else rc.settings.get("auto_commit_additive", True)
    )
    async with session_scope() as s:
        pid = uuid.uuid4()
        await s.execute(
            text(
                "INSERT INTO proposals (id, repo_id, branch, base_commit_id, author_id, message, "
                "source_client, source_model, entries, audit, expires_at) VALUES (:id, :r, :b, "
                ":base, :a, :m, :sc, :sm, CAST(:e AS jsonb), CAST(:au AS jsonb), :exp)"
            ),
            {
                "id": pid,
                "r": rc.id,
                "b": branch,
                "base": base_id,
                "a": actor.id,
                "m": body.message,
                "sc": body.source_client,
                "sm": body.source_model,
                "e": json.dumps({"entries": result.entries}),
                "au": json.dumps({"events": result.audit}, default=str),
                "exp": datetime.now(UTC) + timedelta(seconds=rt.settings.proposal_ttl_s),
            },
        )
    out: dict[str, Any] = {
        "proposal_id": str(pid),
        "repo": rc.name,
        "branch": branch,
        "base_commit": str(base_id),
        "additive_only": result.additive_only,
        "entries": public_entries(result.entries),
        "committed": False,
    }
    if auto and result.additive_only:
        confirmed = await confirm(actor, pid, ConfirmIn())
        out.update(committed=True, commit=confirmed["commit"], counts=confirmed["counts"])
    else:
        out["next_step"] = (
            "Show these entries to the user. Call ctx_confirm with the "
            "proposal_id once they agree (optionally a subset of entry ids, "
            "and choices for flagged entries)."
        )
    return out


async def _load_proposal(
    s: AsyncSession, actor: Actor, proposal_id: uuid.UUID, for_update: bool = False
) -> tuple[Any, RepoCtx]:
    p = (
        (
            await s.execute(
                text(
                    "SELECT * FROM proposals WHERE id = :id" + (" FOR UPDATE" if for_update else "")
                ),
                {"id": proposal_id},
            )
        )
        .mappings()
        .first()
    )
    if p is None:
        raise NotFound("proposal not found")
    rc = await _authorize_by_id(s, actor, p["repo_id"], "viewer")
    if p["author_id"] != actor.id:
        raise NotFound("proposal not found")
    return p, rc


async def get_proposal(actor: Actor, proposal_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        p, rc = await _load_proposal(s, actor, proposal_id)
    return {
        "proposal_id": str(p["id"]),
        "repo": rc.name,
        "branch": p["branch"],
        "status": p["status"],
        "message": p["message"],
        "entries": public_entries(p["entries"]["entries"]),
        "expires_at": p["expires_at"].isoformat(),
        "commit": str(p["result_commit_id"]) if p["result_commit_id"] else None,
    }


async def confirm(actor: Actor, proposal_id: uuid.UUID, body: ConfirmIn) -> dict[str, Any]:
    async with session_scope() as s:
        p, _ = await _load_proposal(s, actor, proposal_id, for_update=True)
        rc = await _authorize_by_id(s, actor, p["repo_id"], "editor")
        if p["status"] != "pending":
            raise Conflict(f"proposal is already {p['status']}")
        if p["expires_at"] < datetime.now(UTC):
            raise Conflict("proposal expired; commit again")
        head = await _branch_head(s, rc.id, p["branch"], for_update=True)
        if head != p["base_commit_id"]:
            raise Conflict(
                f"branch {p['branch']!r} moved since this proposal was made; run the commit "
                "again so the pipeline compares against the new head"
            )
        base = await load_snapshot(s, head)
        commit_id, counts = await apply_proposal(
            s,
            repo_id=rc.id,
            author_id=actor.id,
            branch=p["branch"],
            base=base,
            entries=p["entries"]["entries"],
            audit=p["audit"]["events"],
            message=p["message"],
            source_client=p["source_client"],
            source_model=p["source_model"],
            accept=set(body.accept) if body.accept is not None else None,
            choices=dict(body.choices),
        )
        await s.execute(
            text("UPDATE proposals SET status = 'confirmed', result_commit_id = :c WHERE id = :id"),
            {"c": commit_id, "id": proposal_id},
        )
    return {"commit": str(commit_id), "branch": p["branch"], "counts": counts}


async def discard_proposal(actor: Actor, proposal_id: uuid.UUID) -> None:
    async with session_scope() as s:
        p, _ = await _load_proposal(s, actor, proposal_id, for_update=True)
        if p["status"] == "pending":
            await s.execute(
                text("UPDATE proposals SET status = 'discarded' WHERE id = :id"),
                {"id": proposal_id},
            )


# ---------------------------------------------------------------------------- history


async def log(
    actor: Actor, repo: str, ref: str | None, limit: int = 50, all_branches: bool = False
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        if all_branches:
            starts = [b["head_commit_id"] for b in await _branches(s, rc.id)]
            branch = None
        else:
            branch, head = await _ref_commit(s, actor, rc, ref)
            starts = [head]
        reach: set[uuid.UUID] = set()
        for st in starts:
            reach |= set(await graph.ancestors(s, uuid.UUID(str(st))))
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT c.id, c.message, c.kind, c.source_client, c.source_model, c.created_at, "
                        "u.email AS author, "
                        "COALESCE((SELECT array_agg(p.parent_id ORDER BY p.ord) FROM commit_parents p "
                        "  WHERE p.commit_id = c.id), '{}') AS parents, "
                        "(SELECT count(*) FROM commit_items ci WHERE ci.commit_id = c.id "
                        "  AND ci.status = 'active') AS active_items "
                        "FROM commits c JOIN users u ON u.id = c.author_id "
                        "WHERE c.id = ANY(:ids) ORDER BY c.created_at DESC LIMIT :lim"
                    ),
                    {"ids": list(reach), "lim": limit},
                )
            )
            .mappings()
            .all()
        )
        branches = await _branches(s, rc.id)
    return {
        "branch": branch,
        "commits": [_jsonable(dict(r)) for r in rows],
        "branches": branches,
        "truncated": len(reach) > len(rows),
    }


def diff_snapshots(a: Snapshot | None, b: Snapshot) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "added": [],
        "superseded": [],
        "resolved": [],
        "retracted": [],
        "reactivated": [],
        "removed": [],
    }
    a_items = a.items if a else {}
    for iid, item in b.items.items():
        before = a_items.get(iid)
        if before is None:
            if item.status == "active":
                out["added"].append(item.view())
            continue
        if before.status == item.status:
            continue
        if item.status == "active":
            out["reactivated"].append(item.view())
        elif before.status == "active":
            out[item.status].append(item.view())
    for iid, item in a_items.items():
        if iid not in b.items and item.status == "active":
            out["removed"].append(item.view())
    return out


async def diff(actor: Actor, repo: str, from_ref: str, to_ref: str | None) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        _, a_id = await _ref_commit(s, actor, rc, from_ref)
        _, b_id = await _ref_commit(s, actor, rc, to_ref)
        a = await load_snapshot(s, a_id)
        b = await load_snapshot(s, b_id)
    return {"from": str(a_id), "to": str(b_id), **diff_snapshots(a, b)}


async def show(actor: Actor, repo: str, ref: str | None) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        _, cid = await _ref_commit(s, actor, rc, ref)
        c = (
            (
                await s.execute(
                    text(
                        "SELECT c.*, u.email AS author FROM commits c JOIN users u "
                        "ON u.id = c.author_id WHERE c.id = :c"
                    ),
                    {"c": cid},
                )
            )
            .mappings()
            .one()
        )
        ps = await graph.parents(s, cid)
        snap = await load_snapshot(s, cid)
        parent = await load_snapshot(s, ps[0]) if ps else None
        audit = (
            (
                await s.execute(
                    text(
                        "SELECT stage, decision, details, created_at FROM pipeline_audit "
                        "WHERE commit_id = :c ORDER BY id"
                    ),
                    {"c": cid},
                )
            )
            .mappings()
            .all()
        )
    return {
        "commit": _jsonable(
            {
                k: c[k]
                for k in (
                    "id",
                    "message",
                    "kind",
                    "source_client",
                    "source_model",
                    "created_at",
                    "author",
                )
            }
        ),
        "parents": [str(p) for p in ps],
        "diff": diff_snapshots(parent, snap),
        "audit": [_jsonable(dict(a)) for a in audit],
    }


# ---------------------------------------------------------------------------- branches


async def create_branch(actor: Actor, repo: str, body: BranchCreateIn) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "editor")
        _, start = await _ref_commit(s, actor, rc, body.from_ref)
        exists = await s.execute(
            text("SELECT 1 FROM branches WHERE repo_id = :r AND name = :n"),
            {"r": rc.id, "n": body.name},
        )
        if exists.first():
            raise Conflict(f"branch {body.name!r} already exists")
        await s.execute(
            text(
                "INSERT INTO branches (repo_id, name, head_commit_id, fork_commit_id) "
                "VALUES (:r, :n, :c, :c)"
            ),
            {"r": rc.id, "n": body.name, "c": start},
        )
    return {"branch": body.name, "head": str(start), "fork_point": str(start)}


async def delete_branch(actor: Actor, repo: str, name: str) -> None:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "editor")
        if name == "main":
            raise ValidationFailed("cannot delete main")
        pending = await s.execute(
            text(
                "SELECT 1 FROM merges WHERE repo_id = :r AND ours_branch = :n AND state = 'pending'"
            ),
            {"r": rc.id, "n": name},
        )
        if pending.first():
            raise Conflict("branch has a pending merge; resolve or abort it first")
        res = await s.execute(
            text("DELETE FROM branches WHERE repo_id = :r AND name = :n"), {"r": rc.id, "n": name}
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            raise NotFound(f"branch {name!r} not found")


async def checkout(actor: Actor, repo: str, ref: str) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        branch, commit_id = await _ref_commit(s, actor, rc, ref)
        await _set_head(s, actor.id, rc.id, branch, None if branch else commit_id)
    return {"branch": branch, "head": str(commit_id), "detached": branch is None}


# ---------------------------------------------------------------------------- revert


async def revert(
    actor: Actor, repo: str, commit_ref: str, branch: str | None = None
) -> dict[str, Any]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "editor")
        target_branch = await _write_branch(s, actor, rc, branch)
        head = await _branch_head(s, rc.id, target_branch, for_update=True)
        _, cid = await _ref_commit(s, actor, rc, commit_ref)
        if cid not in await graph.ancestors(s, head):
            raise ValidationFailed(f"{commit_ref} is not in the history of {target_branch!r}")
        ps = await graph.parents(s, cid)
        if not ps:
            raise ValidationFailed("cannot revert the root commit")
        before = await load_snapshot(s, ps[0])  # mainline parent for merges
        after = await load_snapshot(s, cid)
        current = await load_snapshot(s, head)
        membership = current.membership()
        b_mem, a_mem = before.membership(), after.membership()
        conflicts = []
        for iid in set(b_mem) | set(a_mem):
            was, now = b_mem.get(iid), a_mem.get(iid)
            if was == now:
                continue
            cur = membership.get(iid)
            if cur != now:
                item = current.items.get(iid) or after.items.get(iid) or before.items.get(iid)
                conflicts.append(
                    {
                        "item": item.view() if item else str(iid),
                        "reason": "changed again after this commit",
                    }
                )
                continue
            if was is None:
                membership.pop(iid, None)
            else:
                membership[iid] = was
        if conflicts:
            raise Conflict(
                "revert conflicts with later changes; revert those first or commit a fix instead",
                conflicts,
            )
        msg = (
            await s.execute(text("SELECT message FROM commits WHERE id = :c"), {"c": cid})
        ).scalar_one()
        new_id = await graph.insert_commit(
            s,
            repo_id=rc.id,
            author_id=actor.id,
            message=f'Revert "{msg}"',
            kind="revert",
            parent_ids=[head],
            source_client="server",
        )
        await graph.write_membership(s, new_id, membership)
        await graph.move_branch(s, rc.id, target_branch, new_id, expected_head=head)
    return {"commit": str(new_id), "branch": target_branch, "reverted": str(cid)}


# ---------------------------------------------------------------------------- merge


async def _conflict_rows(s: AsyncSession, merge_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        (
            await s.execute(
                text("SELECT * FROM merge_conflicts WHERE merge_id = :m ORDER BY ord"),
                {"m": merge_id},
            )
        )
        .mappings()
        .all()
    )
    ids = {r[k] for r in rows for k in ("base_item", "ours_item", "theirs_item") if r[k]}
    items: dict[uuid.UUID, dict[str, Any]] = {}
    if ids:
        for it in (
            (
                await s.execute(
                    text(
                        "SELECT id, item_key, type, subject, content, attributes FROM state_items "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": list(ids)},
                )
            )
            .mappings()
            .all()
        ):
            items[it["id"]] = {
                "id": str(it["id"]),
                "item_key": str(it["item_key"]),
                "type": it["type"],
                "subject": it["subject"],
                "content": it["content"],
                "reason": (it["attributes"] or {}).get("reason"),
            }
    out = []
    for r in rows:
        out.append(
            {
                "conflict_id": str(r["id"]),
                "kind": r["kind"],
                "base": items.get(r["base_item"]),
                "ours": items.get(r["ours_item"]),
                "theirs": items.get(r["theirs_item"]),
                "reason": (r["details"] or {}).get("reason"),
                "resolution": r["resolution"],
            }
        )
    return out


async def merge(
    actor: Actor, repo: str, source: str, into: str | None = None, review: bool = False
) -> dict[str, Any]:
    rt = get_runtime()
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "editor")
        ours_branch = await _write_branch(s, actor, rc, into)
        pending = (
            await s.execute(
                text(
                    "SELECT id FROM merges WHERE repo_id = :r AND ours_branch = :b "
                    "AND state = 'pending'"
                ),
                {"r": rc.id, "b": ours_branch},
            )
        ).scalar_one_or_none()
        if pending:
            raise Conflict(
                f"a merge into {ours_branch!r} is already pending; resolve or abort it",
                {"merge_id": str(pending)},
            )
        ours_head = await _branch_head(s, rc.id, ours_branch)
        _, theirs_head = await _ref_commit(s, actor, rc, source)
        base_id = await graph.merge_base(s, ours_head, theirs_head)
        if base_id is None:
            raise ValidationFailed("the two refs share no history")
        if base_id == theirs_head:
            return {"result": "up_to_date", "branch": ours_branch, "head": str(ours_head)}
        if base_id == ours_head and not review:
            await graph.move_branch(s, rc.id, ours_branch, theirs_head, expected_head=ours_head)
            return {"result": "fast_forward", "branch": ours_branch, "head": str(theirs_head)}
        base = await load_snapshot(s, base_id)
        ours = await load_snapshot(s, ours_head)
        theirs = await load_snapshot(s, theirs_head)

    with span("ctx.merge_plan", repo=str(rc.id), source=source, into=ours_branch):
        plan = await plan_merge(
            rt.pipeline(),
            base,
            ours,
            theirs,
            llm_enabled=bool(rc.settings.get("llm_enabled", True)),
        )
    auto = rc.settings.get("auto_merge_additive", True) and not review
    async with session_scope() as s:
        merge_id = uuid.uuid4()
        if not plan.conflicts and auto:
            cid = await _write_merge_commit(
                s,
                actor,
                rc,
                ours_branch,
                ours_head,
                theirs_head,
                source,
                plan.membership,
                plan.events,
            )
            await s.execute(
                text(
                    "INSERT INTO merges (id, repo_id, ours_branch, theirs_ref, ours_commit, "
                    "theirs_commit, base_commit, state, plan, result_commit, created_by) VALUES "
                    "(:id, :r, :ob, :tr, :oc, :tc, :bc, 'completed', CAST(:p AS jsonb), :rc, :u)"
                ),
                {
                    "id": merge_id,
                    "r": rc.id,
                    "ob": ours_branch,
                    "tr": source,
                    "oc": ours_head,
                    "tc": theirs_head,
                    "bc": base_id,
                    "p": json.dumps({"summary": plan.summary, "review": plan.review}, default=str),
                    "rc": cid,
                    "u": actor.id,
                },
            )
            return {
                "result": "merged",
                "merge_id": str(merge_id),
                "commit": str(cid),
                "branch": ours_branch,
                "summary": plan.summary,
                "review": plan.review,
            }
        await s.execute(
            text(
                "INSERT INTO merges (id, repo_id, ours_branch, theirs_ref, ours_commit, "
                "theirs_commit, base_commit, state, plan, created_by) VALUES "
                "(:id, :r, :ob, :tr, :oc, :tc, :bc, 'pending', CAST(:p AS jsonb), :u)"
            ),
            {
                "id": merge_id,
                "r": rc.id,
                "ob": ours_branch,
                "tr": source,
                "oc": ours_head,
                "tc": theirs_head,
                "bc": base_id,
                "p": json.dumps(plan_to_json(plan), default=str),
                "u": actor.id,
            },
        )
        for n, c in enumerate(plan.conflicts):
            await s.execute(
                text(
                    "INSERT INTO merge_conflicts (id, merge_id, ord, kind, base_item, "
                    "ours_item, theirs_item, details) VALUES (:id, :m, :o, :k, :b, :ou, :th, "
                    "CAST(:d AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "m": merge_id,
                    "o": n,
                    "k": c.kind,
                    "b": c.base_item,
                    "ou": c.ours_item,
                    "th": c.theirs_item,
                    "d": json.dumps(c.details, default=str),
                },
            )
        conflicts = await _conflict_rows(s, merge_id)
    return {
        "result": "conflicts" if plan.conflicts else "review",
        "merge_id": str(merge_id),
        "branch": ours_branch,
        "summary": plan.summary,
        "conflicts": conflicts,
        "review": plan.review,
        "next_step": (
            "Show each conflict to the user and call ctx_resolve with their choice "
            "(ours, theirs, both, or edited). The merge commit is written when the "
            "last conflict is resolved."
            if plan.conflicts
            else "Review the changes, then complete the merge with ctx_merge "
            "action='complete', or abort it."
        ),
    }


async def _write_merge_commit(
    s: AsyncSession,
    actor: Actor,
    rc: RepoCtx,
    branch: str,
    ours_head: uuid.UUID,
    theirs_head: uuid.UUID,
    source: str,
    membership: dict,
    events: list[dict[str, Any]],
    commit_id: uuid.UUID | None = None,
) -> uuid.UUID:
    cid = commit_id or await graph.insert_commit(
        s,
        repo_id=rc.id,
        author_id=actor.id,
        message=f"Merge {source} into {branch}",
        kind="merge",
        parent_ids=[ours_head, theirs_head],
        source_client="server",
    )
    await graph.write_membership(s, cid, membership)
    if events:
        await s.execute(
            text(
                "INSERT INTO pipeline_audit (commit_id, stage, decision, details) "
                "SELECT :c, x.st, x.de, CAST(x.dt AS jsonb) FROM unnest("
                "CAST(:st AS text[]), CAST(:de AS text[]), CAST(:dt AS text[])) AS x(st, de, dt)"
            ),
            {
                "c": cid,
                "st": [e["stage"] for e in events],
                "de": [e["decision"] for e in events],
                "dt": [json.dumps(e["details"], default=str) for e in events],
            },
        )
    await graph.move_branch(s, rc.id, branch, cid, expected_head=ours_head)
    return cid


async def _load_merge(
    s: AsyncSession, actor: Actor, merge_id: uuid.UUID, need: str
) -> tuple[Any, RepoCtx]:
    m = (
        (await s.execute(text("SELECT * FROM merges WHERE id = :id FOR UPDATE"), {"id": merge_id}))
        .mappings()
        .first()
    )
    if m is None:
        raise NotFound("merge not found")
    rc = await _authorize_by_id(s, actor, m["repo_id"], need)
    return m, rc


async def list_merges(actor: Actor, repo: str, state: str | None = None) -> list[dict[str, Any]]:
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        rows = (
            (
                await s.execute(
                    text(
                        'SELECT id AS merge_id, ours_branch AS branch, theirs_ref AS "from", state, '
                        "result_commit, created_at FROM merges WHERE repo_id = :r "
                        "AND (CAST(:st AS text) IS NULL OR state = :st) "
                        "ORDER BY created_at DESC LIMIT 100"
                    ),
                    {"r": rc.id, "st": state},
                )
            )
            .mappings()
            .all()
        )
    return [_jsonable(dict(r)) for r in rows]


async def get_merge(actor: Actor, merge_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        m, rc = await _load_merge(s, actor, merge_id, "viewer")
        conflicts = await _conflict_rows(s, merge_id)
    plan = m["plan"] or {}
    return {
        "merge_id": str(m["id"]),
        "repo": rc.name,
        "state": m["state"],
        "branch": m["ours_branch"],
        "from": m["theirs_ref"],
        "result_commit": str(m["result_commit"]) if m["result_commit"] else None,
        "summary": plan.get("summary"),
        "review": plan.get("review"),
        "conflicts": conflicts,
    }


async def resolve_conflict(
    actor: Actor, merge_id: uuid.UUID, conflict_id: uuid.UUID, body: ResolveIn
) -> dict[str, Any]:
    async with session_scope() as s:
        m, rc = await _load_merge(s, actor, merge_id, "editor")
        if m["state"] != "pending":
            raise Conflict(f"merge is {m['state']}")
        res = await s.execute(
            text(
                "UPDATE merge_conflicts SET resolution = :r, edited = CAST(:e AS jsonb), "
                "resolved_by = :u WHERE id = :c AND merge_id = :m"
            ),
            {
                "r": body.choice,
                "e": json.dumps(body.edited.model_dump()) if body.edited else None,
                "u": actor.id,
                "c": conflict_id,
                "m": merge_id,
            },
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            raise NotFound("conflict not found in this merge")
        open_ = (
            await s.execute(
                text(
                    "SELECT count(*) FROM merge_conflicts WHERE merge_id = :m AND resolution IS NULL"
                ),
                {"m": merge_id},
            )
        ).scalar_one()
    if open_ == 0:
        return await complete_merge(actor, merge_id)
    async with session_scope() as s:
        conflicts = await _conflict_rows(s, merge_id)
    return {
        "merge_id": str(merge_id),
        "state": "pending",
        "remaining": [c for c in conflicts if c["resolution"] is None],
    }


async def complete_merge(actor: Actor, merge_id: uuid.UUID) -> dict[str, Any]:
    rt = get_runtime()
    async with session_scope() as s:
        m, rc = await _load_merge(s, actor, merge_id, "editor")
        if m["state"] != "pending":
            raise Conflict(f"merge is {m['state']}")
        rows = (
            (
                await s.execute(
                    text("SELECT * FROM merge_conflicts WHERE merge_id = :m ORDER BY ord"),
                    {"m": merge_id},
                )
            )
            .mappings()
            .all()
        )
        if any(r["resolution"] is None for r in rows):
            raise Conflict("resolve every conflict before completing the merge")
        head = await _branch_head(s, rc.id, m["ours_branch"], for_update=True)
        if head != m["ours_commit"]:
            raise Conflict(
                f"branch {m['ours_branch']!r} moved since the merge started; abort and merge again"
            )
        membership = membership_from_json(m["plan"])
        cid = await graph.insert_commit(
            s,
            repo_id=rc.id,
            author_id=actor.id,
            message=f"Merge {m['theirs_ref']} into {m['ours_branch']}",
            kind="merge",
            parent_ids=[m["ours_commit"], m["theirs_commit"]],
            source_client="server",
        )
        theirs_snap = await load_snapshot(s, m["theirs_commit"])
        events = list((m["plan"] or {}).get("events", []))
        for r in rows:
            events.append(
                {
                    "stage": "merge",
                    "decision": f"resolved_{r['resolution']}",
                    "details": {"conflict": str(r["id"]), "kind": r["kind"]},
                }
            )
            await _apply_resolution(s, rt, rc, cid, r, membership, theirs_snap)
        await _write_merge_commit(
            s,
            actor,
            rc,
            m["ours_branch"],
            m["ours_commit"],
            m["theirs_commit"],
            m["theirs_ref"],
            membership,
            events,
            commit_id=cid,
        )
        await s.execute(
            text("UPDATE merges SET state = 'completed', result_commit = :c WHERE id = :id"),
            {"c": cid, "id": merge_id},
        )
    return {
        "merge_id": str(merge_id),
        "state": "completed",
        "commit": str(cid),
        "branch": m["ours_branch"],
    }


async def _apply_resolution(
    s: AsyncSession,
    rt: Any,
    rc: RepoCtx,
    commit_id: uuid.UUID,
    r: Any,
    membership: dict,
    theirs_snap: Snapshot,
) -> None:
    choice = r["resolution"]
    ours_i, theirs_i = r["ours_item"], r["theirs_item"]
    if r["kind"] == "both_modified":
        ours_state = state_from_json(r["details"]["ours_state"])
        theirs_state = state_from_json(r["details"]["theirs_state"])
        for iid in set(ours_state) | set(theirs_state):
            membership.pop(iid, None)
        if choice == "ours":
            membership.update(ours_state)
        elif choice == "theirs":
            membership.update(theirs_state)
        elif choice == "both":
            membership.update(ours_state)
            if theirs_i and theirs_i in theirs_snap.items:
                t = theirs_snap.items[theirs_i]
                clone = NewItem(
                    type=t.type,
                    subject=t.subject,
                    subject_norm=t.subject_norm,
                    content=t.content,
                    reason=t.attributes.get("reason"),
                    compressed=t.compressed,
                    attributes=t.attributes,
                    embedding=t.embedding.tolist() if t.embedding is not None else None,
                    importance=t.importance,
                    llm_score=t.llm_score,
                    pinned=t.pinned,
                    provenance={**t.provenance, "cloned_from": str(t.id)},
                )
                new_id, _ = await insert_item(s, repo_id=rc.id, commit_id=commit_id, item=clone)
                membership[new_id] = ("active", None)
                membership[theirs_i] = ("superseded", new_id)
        else:  # edited
            key = r["details"].get("item_key")
            new_id = await _insert_edited(s, rt, rc, commit_id, r["edited"], key)
            for iid, (st, sup) in {**ours_state, **theirs_state}.items():
                membership[iid] = ("superseded", new_id) if st == "active" else (st, sup)
            membership[new_id] = ("active", None)
        return

    # contradiction / temporal_scope between two different keys
    def repoint(old: uuid.UUID, new: uuid.UUID) -> None:
        """Items that were superseded by the losing side now point at the winner."""
        for iid, (st, sup) in list(membership.items()):
            if sup == old and iid not in (ours_i, theirs_i):
                membership[iid] = (st, new)

    if choice == "ours":
        membership[theirs_i] = ("superseded", ours_i)
        repoint(theirs_i, ours_i)
    elif choice == "theirs":
        membership[ours_i] = ("superseded", theirs_i)
        repoint(ours_i, theirs_i)
    elif choice == "both":
        membership[ours_i] = ("active", None)
        membership[theirs_i] = ("active", None)
    else:
        new_id = await _insert_edited(s, rt, rc, commit_id, r["edited"], None)
        membership[ours_i] = ("superseded", new_id)
        membership[theirs_i] = ("superseded", new_id)
        membership[new_id] = ("active", None)
        repoint(ours_i, new_id)
        repoint(theirs_i, new_id)


async def _insert_edited(
    s: AsyncSession,
    rt: Any,
    rc: RepoCtx,
    commit_id: uuid.UUID,
    edited: dict[str, Any],
    item_key: str | None,
) -> uuid.UUID:
    from .core.items import importance

    subject = normalize_text(edited["subject"])
    content = normalize_text(edited["content"])
    [emb] = await rt.embedder.embed([f"{subject}\n{content}"])
    item = NewItem(
        type=edited["type"],
        subject=subject,
        subject_norm=normalize_subject(subject),
        content=content,
        reason=edited.get("reason"),
        compressed=heuristic_compress(edited["type"], content, edited.get("reason")),
        embedding=emb,
        importance=importance(rt.settings, edited["type"]),
        provenance={"client": "merge_resolution"},
        item_key=item_key,
    )
    new_id, _ = await insert_item(s, repo_id=rc.id, commit_id=commit_id, item=item)
    return new_id


async def abort_merge(actor: Actor, merge_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        m, _ = await _load_merge(s, actor, merge_id, "editor")
        if m["state"] != "pending":
            raise Conflict(f"merge is {m['state']}")
        await s.execute(
            text("UPDATE merges SET state = 'aborted' WHERE id = :id"), {"id": merge_id}
        )
    return {"merge_id": str(merge_id), "state": "aborted"}


# ---------------------------------------------------------------------------- pull


async def pull(actor: Actor, repo: str, body: PullIn) -> dict[str, Any]:
    rt = get_runtime()
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "viewer")
        branch, cid = await _ref_commit(s, actor, rc, body.ref)
        snap = await load_snapshot(s, cid)
        warnings: list[str] = []
        if branch:
            for m in (
                await s.execute(
                    text(
                        "SELECT id, theirs_ref FROM merges WHERE repo_id = :r AND ours_branch = :b "
                        "AND state = 'pending'"
                    ),
                    {"r": rc.id, "b": branch},
                )
            ).all():
                for c in await _conflict_rows(s, m.id):
                    if c["resolution"] is None:
                        o = (c["ours"] or {}).get("content", "(removed)")
                        t = (c["theirs"] or {}).get("content", "(removed)")
                        warnings.append(
                            f"Unresolved merge from {m.theirs_ref}: this branch says "
                            f'"{o}" but {m.theirs_ref} says "{t}".'
                        )
    with span("ctx.render", repo=str(rc.id), model=body.model, budget=body.budget):
        result = await render(
            rt.settings,
            rt.counter,
            snap,
            repo=rc.name,
            branch=branch,
            model=body.model,
            budget=body.budget,
            template=body.template,
            allow_drop_constraints=body.allow_drop_constraints,
            conflicts=warnings,
        )
    used = result.manifest.pop("used_item_ids")
    if used:
        async with session_scope() as s:
            await s.execute(
                text("UPDATE state_items SET use_count = use_count + 1 WHERE id = ANY(:ids)"),
                {"ids": [uuid.UUID(u) for u in used]},
            )
    return {"context": result.text, "manifest": result.manifest}


# ---------------------------------------------------------------------------- imports


async def start_import(
    actor: Actor,
    repo: str,
    data: bytes,
    filename: str | None,
    conversation: str | None,
    branch: str | None,
) -> dict[str, Any]:
    rt = get_runtime()
    if len(data) > rt.settings.max_upload_bytes:
        raise ValidationFailed(f"upload exceeds {rt.settings.max_upload_bytes} bytes")
    source, convs = parse_export(data, filename)
    conv = select_conversation(convs, conversation)
    upload_id = uuid.uuid4()
    async with session_scope() as s:
        rc = await _authorize(s, actor, repo, "editor")
        target = await _write_branch(s, actor, rc, branch)
    uri = await rt.blob.put(rc.id, upload_id, data)
    async with session_scope() as s:
        await s.execute(
            text(
                "INSERT INTO uploads (id, repo_id, created_by, branch, source, filename, "
                "size_bytes, gcs_uri, content, conversation) VALUES (:id, :r, :u, :b, :src, "
                ":fn, :sz, :uri, :content, :conv)"
            ),
            {
                "id": upload_id,
                "r": rc.id,
                "u": actor.id,
                "b": target,
                "src": source,
                "fn": filename,
                "sz": len(data),
                "uri": uri,
                "content": None if uri else data,
                "conv": conv.id or conv.title,
            },
        )
    await tasks.enqueue("import", {"upload_id": str(upload_id)})
    return {
        "upload_id": str(upload_id),
        "source": source,
        "branch": target,
        "conversation": {"id": conv.id, "title": conv.title, "messages": len(conv.messages)},
        "status": "queued",
    }


@tasks.handler("import")
async def _run_import(payload: dict[str, Any]) -> None:
    rt = get_runtime()
    upload_id = uuid.UUID(payload["upload_id"])
    async with session_scope() as s:
        u = (
            (await s.execute(text("SELECT * FROM uploads WHERE id = :id"), {"id": upload_id}))
            .mappings()
            .first()
        )
        if u is None:
            return
        await s.execute(
            text("UPDATE uploads SET status = 'running', error = NULL WHERE id = :id"),
            {"id": upload_id},
        )
        email = (
            await s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": u["created_by"]})
        ).scalar_one()
    try:
        data = u["content"] if u["content"] is not None else await rt.blob.get(u["gcs_uri"])
        _, convs = parse_export(bytes(data), u["filename"])
        conv = select_conversation(convs, u["conversation"])
        items = await extract_items(rt.arbiter, conv, rt.settings.max_items_per_commit)
        if not items:
            raise ValidationFailed("no task state found in this conversation")
        actor = Actor(id=u["created_by"], email=email)
        result = await commit(
            actor,
            CommitIn(
                repo=str(u["repo_id"]),
                message=f"Import: {conv.title}"[:500],
                items=items,
                branch=u["branch"],
                source_client="import",
                source_model=u["source"],
                auto_confirm=False,
            ),
        )
        async with session_scope() as s:
            await s.execute(
                text("UPDATE uploads SET status = 'done', proposal_id = :p WHERE id = :id"),
                {"p": uuid.UUID(result["proposal_id"]), "id": upload_id},
            )
    except Exception as e:
        async with session_scope() as s:
            await s.execute(
                text("UPDATE uploads SET status = 'failed', error = :e WHERE id = :id"),
                {"e": getattr(e, "message", str(e))[:2000], "id": upload_id},
            )
        raise


async def get_import(actor: Actor, upload_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        u = (
            (
                await s.execute(
                    text(
                        "SELECT id, repo_id, created_by, status, error, proposal_id, source, filename, "
                        "conversation, created_at FROM uploads WHERE id = :id"
                    ),
                    {"id": upload_id},
                )
            )
            .mappings()
            .first()
        )
        if u is None:
            raise NotFound("upload not found")
        await _authorize_by_id(s, actor, u["repo_id"], "viewer")
    out = _jsonable(
        {
            k: u[k]
            for k in ("id", "status", "error", "source", "filename", "conversation", "created_at")
        }
    )
    if u["proposal_id"] and u["created_by"] == actor.id:
        out["proposal"] = await get_proposal(actor, u["proposal_id"])
    return out


# ---------------------------------------------------------------------------- helpers


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in d.items():
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, list):
            v = [str(x) if isinstance(x, uuid.UUID) else x for x in v]
        out[k] = v
    return out
