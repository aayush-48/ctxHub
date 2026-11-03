"""SQLAlchemy ORM models. The Alembic migration in migrations/ is the schema of record."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import EMBEDDING_DIM

ITEM_TYPES = ("goal", "constraint", "decision", "open_question", "fact", "task")
ITEM_STATUSES = ("active", "superseded", "resolved", "retracted")
ROLES = ("owner", "editor", "viewer")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, uuid.UUID: UUID(as_uuid=True)}


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    idp_subject: Mapped[str] = mapped_column(Text, unique=True)  # "<provider>:<sub>"
    email: Mapped[str | None] = mapped_column(Text, index=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Repo(Base):
    __tablename__ = "repos"
    # Names are unique across all owners (case-insensitively; see migration 0002).
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoMember(Base):
    __tablename__ = "repo_members"
    __table_args__ = (CheckConstraint("role IN ('owner','editor','viewer')", name="role_valid"),)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text)


class Commit(Base):
    __tablename__ = "commits"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="normal")  # root, normal, merge, revert
    source_client: Mapped[str | None] = mapped_column(Text)
    source_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class CommitParent(Base):
    __tablename__ = "commit_parents"
    commit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("commits.id", ondelete="CASCADE"), primary_key=True
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id"), primary_key=True)
    ord: Mapped[int] = mapped_column(SmallInteger)


class Branch(Base):
    __tablename__ = "branches"
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    head_commit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id"))
    fork_commit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commits.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserHead(Base):
    """Per-user checkout: which branch (or detached commit) a user is on in a repo."""

    __tablename__ = "user_heads"
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    branch: Mapped[str | None] = mapped_column(Text)
    commit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commits.id"))


class StateItem(Base):
    """An immutable version of one typed unit of task state."""

    __tablename__ = "state_items"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    item_key: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    subject_norm: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    compressed: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    llm_score: Mapped[float | None] = mapped_column(Float)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_in: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)


class CommitItem(Base):
    """Membership: the status of every item known to a commit's snapshot.

    Supersession is per snapshot (an item can be superseded on one branch and active on
    another), so the replacement link lives here rather than on state_items.
    """

    __tablename__ = "commit_items"
    commit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("commits.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("state_items.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("state_items.id", ondelete="CASCADE")
    )


class Proposal(Base):
    """A pipeline result waiting for the user to confirm it."""

    __tablename__ = "proposals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(Text)
    base_commit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id", ondelete="CASCADE"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    source_client: Mapped[str | None] = mapped_column(Text)
    source_model: Mapped[str | None] = mapped_column(Text)
    entries: Mapped[dict[str, Any]] = mapped_column()  # {"entries": [...]}
    audit: Mapped[dict[str, Any]] = mapped_column()  # {"events": [...]}
    status: Mapped[str] = mapped_column(Text, default="pending")
    result_commit_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commits.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Merge(Base):
    __tablename__ = "merges"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    ours_branch: Mapped[str] = mapped_column(Text)
    theirs_ref: Mapped[str] = mapped_column(Text)
    ours_commit: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id"))
    theirs_commit: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id"))
    base_commit: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id"))
    state: Mapped[str] = mapped_column(Text)  # pending, completed, aborted
    plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_commit: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commits.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MergeConflict(Base):
    __tablename__ = "merge_conflicts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    merge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merges.id", ondelete="CASCADE"))
    ord: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(Text)  # contradiction, temporal_scope, both_modified
    base_item: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("state_items.id"))
    ours_item: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("state_items.id"))
    theirs_item: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("state_items.id"))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    resolution: Mapped[str | None] = mapped_column(Text)
    edited: Mapped[dict[str, Any] | None] = mapped_column()
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class PipelineAudit(Base):
    __tablename__ = "pipeline_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    commit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commits.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    info: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthPendingAuth(Base):
    """An /authorize request waiting for the user to log in and consent."""

    __tablename__ = "oauth_pending_auth"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String)
    params: Mapped[dict[str, Any]] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthCode(Base):
    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)  # access, refresh
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    client_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scopes: Mapped[dict[str, Any]] = mapped_column()  # {"scopes": [...]}
    resource: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
