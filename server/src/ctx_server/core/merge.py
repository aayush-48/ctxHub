"""Three-way semantic merge over state items.

Changes on one side apply automatically; incompatible changes on the same item or subject go
to the user. Timestamps settle contradictions within a branch but never across branches: a
later commit on another branch is not a later decision on this one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from .pipeline import PipelineDeps, same_group, same_question
from .relations import ItemView, Pair, classify_pairs
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


def _current_of(snap: Snapshot, key: uuid.UUID) -> SnapItem | None:
    """What a branch currently holds for an item: its active version, or, when that branch
    replaced it with a different item, the replacement."""
    item = _active_of(snap, key)
    seen: set[uuid.UUID] = set()
    while item is not None and item.status != "active" and item.superseded_by in snap.items:
        if item.id in seen:
            break
        seen.add(item.id)
        item = snap.items[item.superseded_by]
    return item


def _replacements(snap: Snapshot, state: KeyState | None, key: uuid.UUID) -> list[str]:
    """Items outside this key that a branch used to supersede the key's items."""
    out = []
    for _, _, sup in state or ():
        rep = snap.items.get(sup) if sup else None
        if rep is not None and rep.item_key != key:
            out.append(str(rep.id))
    return sorted(set(out))


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
            bi, oi, ti = _active_of(base, key), _current_of(ours, key), _current_of(theirs, key)
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
                        "ours_replacements": _replacements(ours, o, key),
                        "theirs_replacements": _replacements(theirs, t, key),
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
    same_q: dict[tuple[uuid.UUID, uuid.UUID], bool] = {}
    forced_set = set(forced)
    for a in added_o:
        for b_ in added_t:
            if a.item_key == b_.item_key:
                continue
            is_forced = (a.id, b_.id) in forced_set
            if not same_group(a.type, b_.type) and not is_forced:
                continue
            sim = cosine(a.embedding, b_.embedding)
            # Both branches replaced the same item, or the two items answer the same question.
            same = is_forced or same_question(
                s, a.subject_norm, a.subject_embedding, b_.subject_norm, b_.subject_embedding
            )
            if same or sim >= s.dedupe_threshold:
                pairs.append((a, b_, sim))
                same_q[(a.id, b_.id)] = same
    pairs.sort(key=lambda p: (same_q[(p[0].id, p[1].id)], p[2]), reverse=True)

    exact_idx = {
        n for n, (a, b_, _) in enumerate(pairs) if content_key(a.content) == content_key(b_.content)
    }
    exact = [pairs[n] for n in sorted(exact_idx)]
    to_classify = [p for n, p in enumerate(pairs) if n not in exact_idx]
    # "existing" is ours and "new" is theirs in each pair.
    labels = await classify_pairs(
        [Pair(_view(a), _view(b_), sim, same_q[(a.id, b_.id)]) for a, b_, sim in to_classify],
        s,
        deps.nli,
    )

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
        rel = labels[n]
        base_details = {
            "similarity": round(sim, 4),
            "ours_subject": a.subject,
            "theirs_subject": b_.subject,
        }
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
        confident = rel.confidence >= s.min_confidence
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
