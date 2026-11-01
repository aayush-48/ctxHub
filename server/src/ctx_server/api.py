"""REST API (/api/v1). The CLI and the web viewer use it; MCP tools call the same service."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import ratelimit, service, tasks
from .auth.provider import provider
from .config import get_settings
from .errors import CtxError, Forbidden, ValidationFailed
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
from .service import Actor


class Unauthorized(CtxError):
    status_code = 401
    code = "unauthorized"


async def _actor(request: Request, bucket: str) -> Actor:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token; run `ctx login`")
    tok = await provider.load_access_token(auth[7:].strip())
    if tok is None or not tok.subject:
        raise Unauthorized("invalid or expired token; run `ctx login`")
    actor = Actor(id=uuid.UUID(tok.subject), client_id=tok.client_id)
    await ratelimit.check(actor.id, bucket)
    return actor


async def actor_light(request: Request) -> Actor:
    return await _actor(request, ratelimit.LIGHT)


async def actor_heavy(request: Request) -> Actor:
    return await _actor(request, ratelimit.HEAVY)


Light = Annotated[Actor, Depends(actor_light)]
Heavy = Annotated[Actor, Depends(actor_heavy)]

router = APIRouter(prefix="/api/v1")


class RefIn(BaseModel):
    ref: str = Field(min_length=1, max_length=200)


class RevertIn(BaseModel):
    commit: str = Field(min_length=1, max_length=200)
    branch: str | None = None


class MergeIn(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    into: str | None = None
    review: bool = False


@router.get("/me")
async def me(actor: Light) -> dict[str, Any]:
    return await service.get_user(actor.id)


@router.get("/repos")
async def list_repos(actor: Light) -> list[dict[str, Any]]:
    return await service.list_repos(actor)


@router.post("/repos", status_code=201)
async def create_repo(actor: Light, body: RepoCreateIn) -> dict[str, Any]:
    return await service.create_repo(actor, body)


@router.get("/repos/{repo}")
async def get_repo(actor: Light, repo: str) -> dict[str, Any]:
    return await service.get_repo(actor, repo)


@router.patch("/repos/{repo}/settings")
async def repo_settings(actor: Light, repo: str, body: RepoSettingsIn) -> dict[str, Any]:
    return await service.update_repo_settings(actor, repo, body)


@router.delete("/repos/{repo}", status_code=204)
async def delete_repo(actor: Light, repo: str) -> Response:
    await service.delete_repo(actor, repo)
    return Response(status_code=204)


@router.get("/repos/{repo}/members")
async def members(actor: Light, repo: str) -> list[dict[str, Any]]:
    return await service.list_members(actor, repo)


@router.put("/repos/{repo}/members")
async def set_member(actor: Light, repo: str, body: MemberIn) -> dict[str, Any]:
    return await service.set_member(actor, repo, body)


@router.delete("/repos/{repo}/members/{email}", status_code=204)
async def remove_member(actor: Light, repo: str, email: str) -> Response:
    await service.remove_member(actor, repo, email)
    return Response(status_code=204)


@router.get("/repos/{repo}/status")
async def status(actor: Light, repo: str) -> dict[str, Any]:
    return await service.status(actor, repo)


@router.get("/repos/{repo}/items")
async def items(
    actor: Light, repo: str, ref: str | None = None, all: bool = False
) -> dict[str, Any]:
    return await service.list_items(actor, repo, ref, include_inactive=all)


@router.post("/commits")
async def commit(actor: Heavy, body: CommitIn) -> dict[str, Any]:
    return await service.commit(actor, body)


@router.get("/proposals/{proposal_id}")
async def get_proposal(actor: Light, proposal_id: uuid.UUID) -> dict[str, Any]:
    return await service.get_proposal(actor, proposal_id)


@router.post("/proposals/{proposal_id}/confirm")
async def confirm(actor: Light, proposal_id: uuid.UUID, body: ConfirmIn) -> dict[str, Any]:
    return await service.confirm(actor, proposal_id, body)


@router.delete("/proposals/{proposal_id}", status_code=204)
async def discard(actor: Light, proposal_id: uuid.UUID) -> Response:
    await service.discard_proposal(actor, proposal_id)
    return Response(status_code=204)


@router.get("/repos/{repo}/log")
async def log(
    actor: Light, repo: str, ref: str | None = None, limit: int = 50, all: bool = False
) -> dict[str, Any]:
    return await service.log(actor, repo, ref, limit, all_branches=all)


@router.get("/repos/{repo}/diff")
async def diff(
    actor: Light,
    repo: str,
    from_ref: Annotated[str, Query(alias="from")] = "HEAD~1",
    to: str | None = None,
) -> dict[str, Any]:
    return await service.diff(actor, repo, from_ref, to)


@router.get("/repos/{repo}/commits/{ref:path}")
async def show(actor: Light, repo: str, ref: str) -> dict[str, Any]:
    return await service.show(actor, repo, ref)


@router.get("/repos/{repo}/branches")
async def branches(actor: Light, repo: str) -> list[dict[str, Any]]:
    return (await service.get_repo(actor, repo))["branches"]


@router.post("/repos/{repo}/branches", status_code=201)
async def create_branch(actor: Light, repo: str, body: BranchCreateIn) -> dict[str, Any]:
    return await service.create_branch(actor, repo, body)


@router.delete("/repos/{repo}/branches/{name:path}", status_code=204)
async def delete_branch(actor: Light, repo: str, name: str) -> Response:
    await service.delete_branch(actor, repo, name)
    return Response(status_code=204)


@router.post("/repos/{repo}/checkout")
async def checkout(actor: Light, repo: str, body: RefIn) -> dict[str, Any]:
    return await service.checkout(actor, repo, body.ref)


@router.post("/repos/{repo}/revert")
async def revert(actor: Light, repo: str, body: RevertIn) -> dict[str, Any]:
    return await service.revert(actor, repo, body.commit, body.branch)


@router.post("/repos/{repo}/merges")
async def merge(actor: Heavy, repo: str, body: MergeIn) -> dict[str, Any]:
    return await service.merge(actor, repo, body.source, body.into, body.review)


@router.get("/repos/{repo}/merges")
async def list_merges(actor: Light, repo: str, state: str | None = None) -> list[dict[str, Any]]:
    return await service.list_merges(actor, repo, state)


@router.get("/merges/{merge_id}")
async def get_merge(actor: Light, merge_id: uuid.UUID) -> dict[str, Any]:
    return await service.get_merge(actor, merge_id)


@router.post("/merges/{merge_id}/conflicts/{conflict_id}/resolve")
async def resolve(
    actor: Light, merge_id: uuid.UUID, conflict_id: uuid.UUID, body: ResolveIn
) -> dict[str, Any]:
    return await service.resolve_conflict(actor, merge_id, conflict_id, body)


@router.post("/merges/{merge_id}/complete")
async def complete(actor: Light, merge_id: uuid.UUID) -> dict[str, Any]:
    return await service.complete_merge(actor, merge_id)


@router.post("/merges/{merge_id}/abort")
async def abort(actor: Light, merge_id: uuid.UUID) -> dict[str, Any]:
    return await service.abort_merge(actor, merge_id)


@router.post("/repos/{repo}/pull")
async def pull(actor: Heavy, repo: str, body: PullIn) -> dict[str, Any]:
    return await service.pull(actor, repo, body)


@router.post("/repos/{repo}/imports", status_code=202)
async def start_import(
    actor: Heavy,
    repo: str,
    file: Annotated[UploadFile, File()],
    conversation: Annotated[str | None, Form()] = None,
    branch: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed(f"upload exceeds {limit} bytes")
    return await service.start_import(actor, repo, data, file.filename, conversation, branch)


@router.get("/imports/{upload_id}")
async def get_import(actor: Light, upload_id: uuid.UUID) -> dict[str, Any]:
    return await service.get_import(actor, upload_id)


# -- internal: Cloud Tasks callback

internal = APIRouter(include_in_schema=False)


async def _verify_tasks_oidc(authorization: str | None) -> None:
    s = get_settings()
    if s.tasks_provider != "cloud_tasks":
        raise Forbidden("task callbacks are disabled")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing OIDC token")
    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            authorization[7:],
            g_requests.Request(),
            s.public_base_url.rstrip("/"),
        )
    except ValueError as e:
        raise Unauthorized(f"invalid OIDC token: {e}") from e
    if claims.get("email") != s.tasks_service_account or not claims.get("email_verified"):
        raise Forbidden("task callback from an unexpected identity")


@internal.post("/internal/tasks/{job_id}")
async def run_task(
    job_id: uuid.UUID, authorization: Annotated[str | None, Header()] = None
) -> dict[str, str]:
    await _verify_tasks_oidc(authorization)
    return {"status": await tasks.run_job(job_id)}
