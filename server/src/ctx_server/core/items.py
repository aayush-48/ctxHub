"""New state items: construction, importance scoring, and insertion."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings

TYPE_WEIGHT = {
    "constraint": 1.0,
    "goal": 0.95,
    "decision": 0.8,
    "open_question": 0.7,
    "task": 0.6,
    "fact": 0.5,
}


def importance(
    settings: Settings,
    item_type: str,
    *,
    age_days: float = 0.0,
    use_count: int = 0,
    pinned: bool = False,
) -> float:
    """importance = w_t*typeWeight + w_r*recency + w_u*useCount, normalized
    to 0..1 by the weight sum. Pinned items get the maximum."""
    if pinned:
        return 1.0
    recency = 0.5 ** (max(age_days, 0.0) / settings.recency_half_life_days)
    use = min(1.0, math.log1p(use_count) / math.log1p(20))
    total = settings.w_type + settings.w_recency + settings.w_use
    score = (
        settings.w_type * TYPE_WEIGHT.get(item_type, 0.5)
        + settings.w_recency * recency
        + settings.w_use * use
    )
    return score / total if total else 0.5


@dataclass
class NewItem:
    """An item proposed by the pipeline, serializable into a proposal."""

    type: str
    subject: str
    subject_norm: str
    content: str
    reason: str | None = None
    compressed: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    subject_embedding: list[float] | None = None
    importance: float = 0.5
    llm_score: float | None = None
    pinned: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    item_key: str | None = None  # set when the item is a new version of an existing key

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> NewItem:
        return cls(**d)

    def public(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "subject": self.subject,
            "content": self.content,
            "reason": self.reason,
            "compressed": self.compressed,
            "importance": round(self.importance, 3),
            "pinned": self.pinned,
        }


def _vec_literal(v: list[float] | None) -> str | None:
    return None if v is None else "[" + ",".join(f"{x:.7g}" for x in v) + "]"


async def insert_item(
    session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    commit_id: uuid.UUID,
    item: NewItem,
    item_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a state item; returns (item_id, item_key)."""
    iid = item_id or uuid.uuid4()
    key = uuid.UUID(item.item_key) if item.item_key else uuid.uuid4()
    attributes = dict(item.attributes)
    if item.reason:
        attributes["reason"] = item.reason
    await session.execute(
        text(
            "INSERT INTO state_items (id, repo_id, item_key, type, subject, subject_norm, "
            "content, compressed, attributes, embedding, subject_embedding, importance, llm_score, "
            "pinned, "
            "created_in, provenance) VALUES (:id, :r, :k, :t, :s, :sn, :c, :cmp, "
            "CAST(:attr AS jsonb), CAST(:emb AS vector), CAST(:semb AS vector), :imp, :llm, :pin, :cin, "
            "CAST(:prov AS jsonb))"
        ),
        {
            "id": iid,
            "r": repo_id,
            "k": key,
            "t": item.type,
            "s": item.subject,
            "sn": item.subject_norm,
            "c": item.content,
            "cmp": item.compressed,
            "attr": json.dumps(attributes),
            "emb": _vec_literal(item.embedding),
            "semb": _vec_literal(item.subject_embedding),
            "imp": item.importance,
            "llm": item.llm_score,
            "pin": item.pinned,
            "cin": commit_id,
            "prov": json.dumps(item.provenance),
        },
    )
    return iid, key


def merge_provenance(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Record an extra source for an item that a duplicate was folded into."""
    out = dict(existing)
    seen = list(out.get("also_seen", []))
    seen.append({k: v for k, v in new.items() if k != "also_seen"})
    out["also_seen"] = seen[-20:]
    return out


async def update_provenance(
    session: AsyncSession, item_id: uuid.UUID, provenance: dict[str, Any]
) -> None:
    await session.execute(
        text("UPDATE state_items SET provenance = CAST(:p AS jsonb) WHERE id = :id"),
        {"p": json.dumps(provenance), "id": item_id},
    )
