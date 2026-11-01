"""Loading commit snapshots (item membership with per-commit status)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(eq=False)
class SnapItem:
    id: uuid.UUID
    item_key: uuid.UUID
    type: str
    subject: str
    subject_norm: str
    content: str
    compressed: str | None
    attributes: dict[str, Any]
    embedding: np.ndarray | None
    importance: float
    llm_score: float | None
    pinned: bool
    use_count: int
    created_in: uuid.UUID
    created_at: datetime
    provenance: dict[str, Any]
    status: str = "active"
    superseded_by: uuid.UUID | None = None

    def view(self) -> dict[str, Any]:
        """Public JSON shape of an item."""
        return {
            "id": str(self.id),
            "item_key": str(self.item_key),
            "type": self.type,
            "subject": self.subject,
            "content": self.content,
            "compressed": self.compressed,
            "reason": self.attributes.get("reason"),
            "importance": round(self.importance, 3),
            "pinned": self.pinned,
            "status": self.status,
            "superseded_by": str(self.superseded_by) if self.superseded_by else None,
            "created_in": str(self.created_in),
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Snapshot:
    commit_id: uuid.UUID
    items: dict[uuid.UUID, SnapItem] = field(default_factory=dict)

    def active(self) -> list[SnapItem]:
        return [i for i in self.items.values() if i.status == "active"]

    def by_key(self) -> dict[uuid.UUID, list[SnapItem]]:
        out: dict[uuid.UUID, list[SnapItem]] = {}
        for i in self.items.values():
            out.setdefault(i.item_key, []).append(i)
        return out

    def membership(self) -> dict[uuid.UUID, tuple[str, uuid.UUID | None]]:
        return {i.id: (i.status, i.superseded_by) for i in self.items.values()}

    def copy(self) -> Snapshot:
        return Snapshot(self.commit_id, {k: replace(v) for k, v in self.items.items()})


_SNAPSHOT_SQL = text(
    """
    SELECT si.id, si.item_key, si.type, si.subject, si.subject_norm, si.content, si.compressed,
           si.attributes, si.embedding, si.importance, si.llm_score, si.pinned, si.use_count,
           si.created_in, si.created_at, si.provenance, ci.status, ci.superseded_by
    FROM commit_items ci JOIN state_items si ON si.id = ci.item_id
    WHERE ci.commit_id = :cid
    """
)


def _vec(v: Any) -> np.ndarray | None:
    if v is None:
        return None
    if isinstance(v, str):  # pgvector text form "[1,2,3]" when no codec is registered
        return np.asarray([float(x) for x in v.strip("[]").split(",")], dtype=np.float64)
    return np.asarray(v, dtype=np.float64)


async def load_snapshot(session: AsyncSession, commit_id: uuid.UUID) -> Snapshot:
    rows = (await session.execute(_SNAPSHOT_SQL, {"cid": commit_id})).mappings().all()
    snap = Snapshot(commit_id)
    for r in rows:
        snap.items[r["id"]] = SnapItem(
            id=r["id"],
            item_key=r["item_key"],
            type=r["type"],
            subject=r["subject"],
            subject_norm=r["subject_norm"],
            content=r["content"],
            compressed=r["compressed"],
            attributes=r["attributes"] or {},
            embedding=_vec(r["embedding"]),
            importance=float(r["importance"]),
            llm_score=r["llm_score"],
            pinned=r["pinned"],
            use_count=r["use_count"],
            created_in=r["created_in"],
            created_at=r["created_at"],
            provenance=r["provenance"] or {},
            status=r["status"],
            superseded_by=r["superseded_by"],
        )
    return snap


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
