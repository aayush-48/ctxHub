"""Three-way semantic merge over state items.

Changes on one side apply automatically; incompatible changes on the same item or subject go
to the user. Timestamps settle contradictions within a branch but never across branches: a
later commit on another branch is not a later decision on this one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from ..providers.arbiter import ArbiterUnavailable, ItemView
from .pipeline import PipelineDeps
from .snapshot import SnapItem, Snapshot, cosine
from .text import content_key

Membership = dict[uuid.UUID, tuple[str, uuid.UUID | None]]
KeyState = frozenset[tuple[uuid.UUID, str, uuid.UUID | None]]


@dataclass
class ConflictPlan:
    kind: str  # both_modified, contradiction, temporal_scope
    base_item: uuid.UUID | None
    ours_item: uuid.UUID | None
    theirs_item: uuid.UUID | None
    details: dict[str, Any]


@dataclass
class MergePlan:
    membership: Membership
    conflicts: list[ConflictPlan] = field(default_factory=list)
    review: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def _key_states(s: Snapshot) -> dict[uuid.UUID, KeyState]:
    out: dict[uuid.UUID, set] = {}
    for i in s.items.values():
        out.setdefault(i.item_key, set()).add((i.id, i.status, i.superseded_by))
    return {k: frozenset(v) for k, v in out.items()}


def _active_of(snap: Snapshot, key: uuid.UUID) -> SnapItem | None:
    cands = [i for i in snap.items.values() if i.item_key == key]
    if not cands:
        return None
    act = [i for i in cands if i.status == "active"]
    return max(act or cands, key=lambda i: i.created_at)


def _state_json(state: KeyState | None) -> list[list[str | None]]:
    return [
        [str(i), st, str(sup) if sup else None]
        for i, st, sup in sorted(state or (), key=lambda x: str(x[0]))
    ]


def _view(i: SnapItem) -> ItemView:
    return ItemView(
        type=i.type, subject=i.subject, content=i.content, reason=i.attributes.get("reason")
    )


async def plan_merge(
    deps: PipelineDeps,
    base: Snapshot,
    ours: Snapshot,
    theirs: Snapshot,
    *,
    llm_enabled: bool,
) -> MergePlan:
    s = deps.settings
    kb, ko, kt = _key_states(base), _key_states(ours), _key_states(theirs)
    membership: Membership = {}
    plan = MergePlan(membership=membership)
    summary = {
        "from_ours": 0,
        "from_theirs": 0,
        "same_both": 0,
        "conflicts": 0,
        "deduplicated": 0,
        "refined": 0,
        "kept_both": 0,
    }

    def take(state: KeyState) -> None:
        for iid, st, sup in state:
            membership[iid] = (st, sup)

    def statuses(state: KeyState | None) -> frozenset | None:
        return frozenset((i, st) for i, st, _ in state) if state is not None else None

    # Replacement items that each side linked the same superseded item to; they are compared
    # by meaning below instead of counting as a conflict on the old item.
    forced: list[tuple[uuid.UUID, uuid.UUID]] = []

    # -- Per item_key three-way comparison
    for key in set(kb) | set(ko) | set(kt):
        b, o, t = kb.get(key), ko.get(key), kt.get(key)
        if o != t and o is not None and t is not None and statuses(o) == statuses(t):
            # Both sides closed the same items, only pointing at different replacements.
            take(o)
            t_links = {i: sup for i, _, sup in t}
            for i, _, sup_o in o:
                sup_t = t_links.get(i)
                if sup_o and sup_t and sup_o != sup_t:
                    forced.append((sup_o, sup_t))
            summary["same_both"] += 1
            continue
        if o == t:
            if o:
                take(o)
            if o != b:
                summary["same_both"] += 1
        elif o == b:
            if t:
                take(t)
            summary["from_theirs"] += 1
        elif t == b:
            if o:
                take(o)
            summary["from_ours"] += 1
        else:
            # Both sides changed the same item differently. Keep ours provisionally.
            if o:
                take(o)
            bi, oi, ti = _active_of(base, key), _active_of(ours, key), _active_of(theirs, key)
            plan.conflicts.append(
                ConflictPlan(
                    kind="both_modified",
                    base_item=bi.id if bi else None,
                    ours_item=oi.id if oi else None,
                    theirs_item=ti.id if ti else None,
                    details={
                        "item_key": str(key),
                        "ours_state": _state_json(o),
                        "theirs_state": _state_json(t),
                        "reason": "both branches changed this item",
                    },
                )
            )
            plan.events.append(
                {"stage": "merge", "decision": "both_modified", "details": {"item_key": str(key)}}
            )

    # Theirs may supersede an item by pointing at a theirs-only item, etc. Every referenced
    # item is in membership because each key's state is taken whole.

    # -- Different keys, same subject: items added on each side since the base
    in_conflict = {c.ours_item for c in plan.conflicts} | {c.theirs_item for c in plan.conflicts}
    added_o = [i for i in ours.active() if i.item_key not in kb and i.id not in in_conflict]
    added_t = [i for i in theirs.active() if i.item_key not in kb and i.id not in in_conflict]
    pairs: list[tuple[SnapItem, SnapItem, float]] = []
    forced_set = set(forced)
    for a in added_o:
        for b_ in added_t:
            if a.item_key == b_.item_key:
                continue
            is_forced = (a.id, b_.id) in forced_set
            if a.type != b_.type and not is_forced:
                continue
            sim = cosine(a.embedding, b_.embedding)
            if is_forced or a.subject_norm == b_.subject_norm or sim >= s.related_threshold:
                pairs.append((a, b_, sim))
    pairs.sort(key=lambda p: (p[0].subject_norm == p[1].subject_norm, p[2]), reverse=True)

    exact_idx = {
        n for n, (a, b_, _) in enumerate(pairs) if content_key(a.content) == content_key(b_.content)
    }
    exact = [pairs[n] for n in sorted(exact_idx)]
    to_classify = [p for n, p in enumerate(pairs) if n not in exact_idx]
    labels: dict[int, Any] = {}
    arbiter_error = None
    if to_classify and llm_enabled and deps.arbiter.available:
        try:
            labels = dict(
                enumerate(
                    await deps.arbiter.classify([(_view(a), _view(b_)) for a, b_, _ in to_classify])
                )
            )
        except ArbiterUnavailable as e:
            arbiter_error = e.message
    elif to_classify:
        arbiter_error = "LLM arbiter disabled for this repo"

    used: set[uuid.UUID] = set()
    for a, b_, _sim in exact:
        if a.id in used or b_.id in used:
            continue
        used |= {a.id, b_.id}
        membership[b_.id] = ("superseded", a.id)
        summary["deduplicated"] += 1
        plan.events.append(
            {
                "stage": "merge",
                "decision": "duplicate",
                "details": {"ours": str(a.id), "theirs": str(b_.id), "exact": True},
            }
        )

    for n, (a, b_, sim) in enumerate(to_classify):
        if a.id in used or b_.id in used:
            continue
        rel = labels.get(n)
        base_details = {
            "similarity": round(sim, 4),
            "ours_subject": a.subject,
            "theirs_subject": b_.subject,
        }
        if rel is None:
            if a.subject_norm == b_.subject_norm:
                used |= {a.id, b_.id}
                plan.conflicts.append(
                    ConflictPlan(
                        kind="contradiction",
                        base_item=None,
                        ours_item=a.id,
                        theirs_item=b_.id,
                        details={
                            **base_details,
                            "label": "unclassified",
                            "reason": f"same subject on both branches; {arbiter_error}",
                        },
                    )
                )
            continue
        plan.events.append(
            {
                "stage": "merge",
                "decision": rel.label,
                "details": {
                    "ours": str(a.id),
                    "theirs": str(b_.id),
                    "confidence": rel.confidence,
                    "refined": rel.refined,
                    "reason": rel.reason,
                },
            }
        )
        confident = rel.confidence >= s.arbiter_min_confidence
        if rel.label in ("contradiction", "temporal_scope"):
            used |= {a.id, b_.id}
            plan.conflicts.append(
                ConflictPlan(
                    kind=rel.label,
                    base_item=None,
                    ours_item=a.id,
                    theirs_item=b_.id,
                    details={
                        **base_details,
                        "label": rel.label,
                        "confidence": rel.confidence,
                        "reason": rel.reason,
                    },
                )
            )
        elif rel.label == "duplicate" and confident:
            used |= {a.id, b_.id}
            membership[b_.id] = ("superseded", a.id)
            summary["deduplicated"] += 1
        elif rel.label == "refinement" and confident:
            used |= {a.id, b_.id}
            # "existing" is ours (the first item of the pair), "new" is theirs.
            keep, drop = (a, b_) if rel.refined == "existing" else (b_, a)
            membership[drop.id] = ("superseded", keep.id)
            for iid, (st, sup) in list(membership.items()):
                if sup == drop.id and iid != drop.id:
                    membership[iid] = (st, keep.id)
            summary["refined"] += 1
            plan.review.append(
                {
                    "kept": keep.view(),
                    "superseded": drop.view(),
                    "reason": rel.reason,
                    "note": "kept the refined item; review it",
                }
            )
        else:
            summary["kept_both"] += 1

    summary["conflicts"] = len(plan.conflicts)
    plan.summary = summary
    return plan


def plan_to_json(plan: MergePlan) -> dict[str, Any]:
    return {
        "membership": {
            str(k): [st, str(sup) if sup else None] for k, (st, sup) in plan.membership.items()
        },
        "review": plan.review,
        "events": plan.events,
        "summary": plan.summary,
    }


def membership_from_json(d: dict[str, Any]) -> Membership:
    return {
        uuid.UUID(k): (v[0], uuid.UUID(v[1]) if v[1] else None)
        for k, v in d.get("membership", {}).items()
    }


def state_from_json(rows: list[list[str | None]]) -> Membership:
    return {uuid.UUID(r[0]): (r[1], uuid.UUID(r[2]) if r[2] else None) for r in rows}  # type: ignore[arg-type]
