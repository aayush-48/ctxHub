"""The cleaning pipeline: turns a commit request into a proposed diff, and applies a confirmed
proposal as a commit.

Stages: validate (pydantic, before this module), normalize, embed, dedupe, classify relations,
supersede, score importance, compress. Cheap checks run first so the LLM arbiter only sees the
pairs that need judgment. Every decision is recorded as an audit event.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..errors import ValidationFailed
from ..providers.arbiter import Arbiter, ArbiterUnavailable, ItemView, Relation
from ..providers.embeddings import Embedder
from ..providers.nli import NLI
from ..schemas import ItemIn, StatusUpdateIn
from . import graph
from .items import NewItem, importance, insert_item, merge_provenance, update_provenance
from .snapshot import SnapItem, Snapshot, cosine
from .text import content_key, heuristic_compress, normalize_subject, normalize_text

log = logging.getLogger(__name__)

# Actions whose entry carries a new item.
ITEM_ACTIONS = {"add", "supersede", "refine", "flag", "drop_duplicate"}
ADDITIVE_ACTIONS = {"add", "drop_duplicate"}
# Types that can close an open question.
ANSWER_TYPES = {"decision", "fact"}

_NEGATION = re.compile(r"\b(not|no|never|without|don't|doesn't|won't|cannot|can't|isn't)\b", re.I)
_DIGIT_TOKEN = re.compile(r"\b\S*\d\S*\b")


@dataclass
class PipelineDeps:
    settings: Settings
    embedder: Embedder
    arbiter: Arbiter
    nli: NLI | None = None


@dataclass
class PipelineResult:
    entries: list[dict[str, Any]]
    audit: list[dict[str, Any]]

    @property
    def additive_only(self) -> bool:
        return all(e["action"] in ADDITIVE_ACTIONS for e in self.entries)


@dataclass
class _Cand:
    """A comparison target: an active snapshot item or an earlier item of the same commit."""

    view: ItemView
    subject_norm: str
    embedding: Any
    snap: SnapItem | None = None
    entry_id: str | None = None

    def target(self) -> dict[str, Any]:
        if self.snap is not None:
            return {
                "item_id": str(self.snap.id),
                "item_key": str(self.snap.item_key),
                "type": self.snap.type,
                "subject": self.snap.subject,
                "content": self.snap.content,
            }
        return {"entry": self.entry_id, "subject": self.view.subject, "content": self.view.content}


@dataclass
class _Work:
    eid: str
    item: NewItem
    replaces: str | None
    entry: dict[str, Any] | None = None
    pairs: list[tuple[_Cand, float]] = field(default_factory=list)  # (candidate, similarity)


def find_item(snapshot: Snapshot, ref: str, *, active_only: bool = True) -> SnapItem:
    """Find an item by id or item_key, full or by unique prefix (>= 6 hex chars)."""
    r = ref.strip().lower().replace("-", "")
    if len(r) < 6 or not re.fullmatch(r"[0-9a-f]+", r):
        raise ValidationFailed(f"item reference {ref!r} must be at least 6 hex characters")
    pool = snapshot.active() if active_only else list(snapshot.items.values())
    matches = {i.id: i for i in pool if i.id.hex.startswith(r) or i.item_key.hex.startswith(r)}
    if not matches:
        raise ValidationFailed(f"no {'active ' if active_only else ''}item matches {ref!r}")
    if len(matches) > 1:
        raise ValidationFailed(f"item reference {ref!r} is ambiguous")
    return next(iter(matches.values()))


def _material_difference(a: str, b: str) -> bool:
    """Near-identical texts that differ in numbers/identifiers or negation are not duplicates."""
    da = set(_DIGIT_TOKEN.findall(a.lower()))
    db = set(_DIGIT_TOKEN.findall(b.lower()))
    na = bool(_NEGATION.search(a))
    nb = bool(_NEGATION.search(b))
    return da != db or na != nb


def _view(item: NewItem) -> ItemView:
    return ItemView(type=item.type, subject=item.subject, content=item.content, reason=item.reason)


def _snap_view(s: SnapItem) -> ItemView:
    return ItemView(
        type=s.type, subject=s.subject, content=s.content, reason=s.attributes.get("reason")
    )


async def run_pipeline(
    deps: PipelineDeps,
    snapshot: Snapshot,
    items: list[ItemIn],
    updates: list[StatusUpdateIn],
    provenance: dict[str, Any],
    *,
    llm_enabled: bool = True,
) -> PipelineResult:
    s = deps.settings
    audit: list[dict[str, Any]] = []
    arbiter_ok = llm_enabled and deps.arbiter.available

    def event(stage: str, decision: str, **details: Any) -> None:
        audit.append({"stage": stage, "decision": decision, "details": details})

    # -- Resolve explicit references first, so bad references fail fast with field errors.
    errors: list[dict[str, str]] = []
    replace_targets: dict[int, SnapItem] = {}
    for i, it in enumerate(items):
        if it.replaces:
            try:
                replace_targets[i] = find_item(snapshot, it.replaces)
            except ValidationFailed as e:
                errors.append({"field": f"items.{i}.replaces", "error": e.message})
    status_targets: list[SnapItem] = []
    for i, up in enumerate(updates):
        try:
            status_targets.append(find_item(snapshot, up.item))
        except ValidationFailed as e:
            errors.append({"field": f"updates.{i}.item", "error": e.message})
    if errors:
        raise ValidationFailed("invalid item references", errors)

    # -- Normalize
    now = datetime.now(UTC).isoformat()
    work: list[_Work] = []
    for i, it in enumerate(items):
        subject = normalize_text(it.subject)
        content = normalize_text(it.content)
        work.append(
            _Work(
                eid=f"e{i + 1}",
                replaces=it.replaces,
                item=NewItem(
                    type=it.type,
                    subject=subject,
                    subject_norm=normalize_subject(subject),
                    content=content,
                    reason=normalize_text(it.reason) if it.reason else None,
                    attributes=dict(it.attributes),
                    pinned=it.pinned,
                    provenance={**provenance, "committed_at": now},
                ),
            )
        )
        event("normalize", "ok", entry=f"e{i + 1}", subject_norm=work[-1].item.subject_norm)

    # -- Embed
    if work:
        vecs = await deps.embedder.embed([f"{w.item.subject}\n{w.item.content}" for w in work])
        for w, v in zip(work, vecs, strict=True):
            w.item.embedding = v
        event("embed", "ok", count=len(work), provider=deps.embedder.name)

    active = snapshot.active()
    arbiter_pairs: list[tuple[_Work, _Cand, float]] = []
    nli_queue: list[tuple[_Work, _Cand, float]] = []

    # -- Dedupe and candidate selection
    for idx, w in enumerate(work):
        if idx in replace_targets:
            t = replace_targets[idx]
            same_type = t.type == w.item.type
            if same_type:
                w.item.item_key = str(t.item_key)
            w.entry = {
                "action": "refine" if same_type else "supersede",
                "target": _Cand(_snap_view(t), t.subject_norm, t.embedding, snap=t).target(),
                "relation": "explicit",
                "confidence": 1.0,
                "reason": "explicitly replaces an existing item",
            }
            event("supersede", "explicit", entry=w.eid, target=str(t.id))
            continue

        wv = np.asarray(w.item.embedding) if w.item.embedding is not None else None
        pool: list[_Cand] = [
            _Cand(_snap_view(a), a.subject_norm, a.embedding, snap=a)
            for a in active
            if a.type == w.item.type
        ]
        pool += [
            _Cand(_view(p.item), p.item.subject_norm, np.asarray(p.item.embedding), entry_id=p.eid)
            for p in work[:idx]
            if p.item.type == w.item.type
            and not (p.entry and p.entry["action"] == "drop_duplicate")
        ]
        scored = []
        for c in pool:
            sim = cosine(wv, c.embedding)
            same_subject = c.subject_norm == w.item.subject_norm
            if content_key(c.view.content) == content_key(w.item.content):
                w.entry = {
                    "action": "drop_duplicate",
                    "target": c.target(),
                    "relation": "duplicate",
                    "confidence": 1.0,
                    "similarity": round(sim, 4),
                    "reason": "identical content",
                }
                event("dedupe", "exact_duplicate", entry=w.eid, target=c.target())
                break
            if same_subject or sim >= s.related_threshold:
                scored.append((c, sim, same_subject))
        if w.entry is not None:
            continue
        scored.sort(key=lambda x: (x[2], x[1]), reverse=True)
        for c, sim, _same in scored[:3]:
            if sim >= s.dedupe_threshold and not _material_difference(
                c.view.content, w.item.content
            ):
                if deps.nli is not None:
                    nli_queue.append((w, c, sim))
                    continue
                w.entry = {
                    "action": "drop_duplicate",
                    "target": c.target(),
                    "relation": "duplicate",
                    "confidence": round(sim, 4),
                    "similarity": round(sim, 4),
                    "reason": "near-identical to an existing item",
                }
                event("dedupe", "similar_duplicate", entry=w.eid, similarity=sim)
                break
            w.pairs.append((c, sim))

    # -- NLI prefilter (optional): confirms high-similarity duplicates, drops clearly
    # unrelated borderline pairs before they reach the LLM.
    if deps.nli is not None:
        borderline = [(w, c, sim) for w in work if w.entry is None for c, sim in w.pairs]
        queue = [x for x in nli_queue if x[0].entry is None] + borderline
        if queue:
            scores = await deps.nli.score([(c.view.content, w.item.content) for w, c, _ in queue])
            for (w, c, sim), sc in zip(queue, scores, strict=True):
                if w.entry is not None:
                    continue
                if sim >= s.dedupe_threshold:
                    if sc.contradiction < 0.5:
                        w.entry = {
                            "action": "drop_duplicate",
                            "target": c.target(),
                            "relation": "duplicate",
                            "confidence": round(sc.entailment, 4),
                            "similarity": round(sim, 4),
                            "reason": "near-identical; NLI finds no contradiction",
                        }
                        event("dedupe", "nli_duplicate", entry=w.eid, nli=sc.__dict__)
                    else:
                        w.pairs.insert(0, (c, sim))
                        event("classify", "nli_contradiction_suspected", entry=w.eid)
                elif sc.neutral > 0.9 and c.subject_norm != w.item.subject_norm:
                    w.pairs = [p for p in w.pairs if p[0] is not c]
                    event("classify", "nli_unrelated", entry=w.eid, nli=sc.__dict__)

    for w in work:
        if w.entry is None:
            arbiter_pairs += [(w, c, sim) for c, sim in w.pairs]

    # -- Classify relations with the arbiter
    relations: dict[int, Relation] = {}
    arbiter_error: str | None = None
    if arbiter_pairs and arbiter_ok:
        try:
            results = await deps.arbiter.classify(
                [(c.view, _view(w.item)) for w, c, _ in arbiter_pairs]
            )
            relations = dict(enumerate(results))
            for n, (w, c, sim) in enumerate(arbiter_pairs):
                r = relations[n]
                event(
                    "classify",
                    r.label,
                    entry=w.eid,
                    target=c.target(),
                    similarity=round(sim, 4),
                    confidence=r.confidence,
                    refined=r.refined,
                    reason=r.reason,
                    arbiter=deps.arbiter.name,
                )
        except ArbiterUnavailable as e:
            arbiter_error = e.message
            event("classify", "arbiter_unavailable", error=e.message)
    elif arbiter_pairs:
        arbiter_error = "LLM arbiter disabled for this repo"

    # -- Supersede: turn relations into one primary entry per item
    min_conf = s.arbiter_min_confidence
    for w in work:
        if w.entry is not None:
            continue
        mine = [(n, c, sim) for n, (ww, c, sim) in enumerate(arbiter_pairs) if ww is w]
        if not mine:
            w.entry = {"action": "add", "reason": "no related active item"}
            continue
        if arbiter_error is not None:
            same = [(c, sim) for _, c, sim in mine if c.subject_norm == w.item.subject_norm]
            if same:
                c, sim = same[0]
                w.entry = {
                    "action": "flag",
                    "target": c.target(),
                    "relation": "unclassified",
                    "similarity": round(sim, 4),
                    "reason": f"same subject as an existing item; not classified ({arbiter_error})",
                    "default_choice": "keep_both",
                }
            else:
                w.entry = {"action": "add", "reason": "related items found but not classified"}
            continue
        w.entry = _decide(w, [(relations[n], c, sim) for n, c, sim in mine], min_conf)
        event("supersede", w.entry["action"], entry=w.eid, relation=w.entry.get("relation"))

    # -- Open questions answered by new decisions or facts
    entries_extra: list[dict[str, Any]] = []
    open_qs = [a for a in active if a.type == "open_question"]
    status_target_ids = {t.id for t in status_targets}
    q_pairs: list[tuple[_Work, SnapItem]] = []
    for w in work:
        if w.item.type not in ANSWER_TYPES or (w.entry and w.entry["action"] == "drop_duplicate"):
            continue
        wv = np.asarray(w.item.embedding)
        for q in open_qs:
            if q.id in status_target_ids:
                continue
            if (
                q.subject_norm == w.item.subject_norm
                or cosine(wv, q.embedding) >= s.related_threshold
            ):
                q_pairs.append((w, q))
    if q_pairs and arbiter_ok:
        try:
            verdicts = await deps.arbiter.answers(
                [(_snap_view(q), _view(w.item)) for w, q in q_pairs]
            )
            claimed: set[uuid.UUID] = set()
            for (w, q), v in zip(q_pairs, verdicts, strict=True):
                event(
                    "classify",
                    "answers" if v.answers else "does_not_answer",
                    entry=w.eid,
                    question=str(q.id),
                    confidence=v.confidence,
                    reason=v.reason,
                )
                if v.answers and v.confidence >= min_conf and q.id not in claimed:
                    claimed.add(q.id)
                    entries_extra.append(
                        {
                            "action": "resolve",
                            "item_entry": w.eid,
                            "target": _Cand(_snap_view(q), q.subject_norm, None, snap=q).target(),
                            "relation": "answers",
                            "confidence": v.confidence,
                            "reason": v.reason or "answers the open question",
                        }
                    )
        except ArbiterUnavailable as e:
            event("classify", "arbiter_unavailable", stage_detail="answers", error=e.message)

    # -- Score importance and compress
    kept = [w for w in work if w.entry and w.entry["action"] != "drop_duplicate"]
    scores = None
    if kept and arbiter_ok:
        try:
            scores = await deps.arbiter.score([_view(w.item) for w in kept])
        except ArbiterUnavailable as e:
            event("score", "arbiter_unavailable", error=e.message)
    for n, w in enumerate(kept):
        it = w.item
        if scores is not None:
            it.llm_score = scores[n].importance
            it.compressed = scores[n].compressed.strip() or None
        if it.type in ("constraint", "open_question") or not it.compressed:
            it.compressed = heuristic_compress(it.type, it.content, it.reason)
        if len(it.compressed) > len(it.content):
            it.compressed = it.content
        it.importance = importance(s, it.type, llm_score=it.llm_score, pinned=it.pinned)
        event(
            "score", "ok", entry=w.eid, importance=round(it.importance, 4), llm_score=it.llm_score
        )
        event("compress", "llm" if scores is not None else "heuristic", entry=w.eid)

    # -- Assemble the diff
    entries: list[dict[str, Any]] = []
    for w in work:
        assert w.entry is not None
        entries.append({"id": w.eid, **w.entry, "item": w.item.to_json()})
    for n, e in enumerate(entries_extra):
        entries.append({"id": f"r{n + 1}", **e})
    for n, (up, t) in enumerate(zip(updates, status_targets, strict=True)):
        entries.append(
            {
                "id": f"s{n + 1}",
                "action": "status",
                "status": up.status,
                "note": up.note,
                "target": _Cand(_snap_view(t), t.subject_norm, None, snap=t).target(),
                "reason": up.note or f"marked {up.status}",
            }
        )
    return PipelineResult(entries=entries, audit=audit)


_PRIORITY = {"duplicate": 0, "refinement": 1, "contradiction": 2, "temporal_scope": 3}


def _decide(w: _Work, rels: list[tuple[Relation, _Cand, float]], min_conf: float) -> dict:
    """Pick the primary action for one new item from its classified pairs."""
    meaningful = [(r, c, sim) for r, c, sim in rels if r.label in _PRIORITY]
    if not meaningful:
        return {"action": "add", "reason": "related items are complementary or unrelated"}
    meaningful.sort(key=lambda x: (_PRIORITY[x[0].label], -x[0].confidence))
    r, c, sim = meaningful[0]
    base = {
        "target": c.target(),
        "relation": r.label,
        "confidence": round(r.confidence, 4),
        "similarity": round(sim, 4),
        "reason": r.reason,
    }
    if r.confidence < min_conf:
        return {
            **base,
            "action": "flag",
            "default_choice": "keep_both",
            "reason": f"low-confidence {r.label}: {r.reason}".strip(": "),
        }
    if r.label == "temporal_scope":
        return {**base, "action": "flag", "default_choice": "keep_both"}
    if c.snap is None:
        # Two items of the same commit relate. Duplicates drop; anything else is the user's call.
        if r.label == "duplicate":
            return {**base, "action": "drop_duplicate"}
        return {**base, "action": "flag", "default_choice": "keep_both"}
    if r.label == "duplicate":
        return {**base, "action": "drop_duplicate"}
    if r.label == "refinement":
        if r.refined == "existing":
            return {
                **base,
                "action": "drop_duplicate",
                "reason": r.reason or "the existing item is already more specific",
            }
        w.item.item_key = str(c.snap.item_key)
        return {**base, "action": "refine"}
    return {**base, "action": "supersede"}


def public_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diff entries without internal fields (embeddings, normalized subjects)."""
    out = []
    for e in entries:
        pe = {k: v for k, v in e.items() if k != "item"}
        if "item" in e:
            pe["item"] = NewItem.from_json(e["item"]).public()
        out.append(pe)
    return out


async def apply_proposal(
    session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    author_id: uuid.UUID,
    branch: str,
    base: Snapshot,
    entries: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    message: str,
    source_client: str | None,
    source_model: str | None,
    accept: set[str] | None,
    choices: dict[str, str],
) -> tuple[uuid.UUID, dict[str, int]]:
    """Write the accepted entries as one commit on top of `base` and move the branch head.
    Must run inside a transaction; the branch move is a compare-and-swap on base."""
    known_ids = {e["id"] for e in entries}
    unknown = ((accept or set()) - known_ids) | (set(choices) - known_ids)
    if unknown:
        raise ValidationFailed(f"unknown entry ids: {sorted(unknown)}")

    def accepted(e: dict[str, Any]) -> bool:
        return accept is None or e["id"] in accept

    commit_id = await graph.insert_commit(
        session,
        repo_id=repo_id,
        author_id=author_id,
        message=message,
        kind="normal",
        parent_ids=[base.commit_id],
        source_client=source_client,
        source_model=source_model,
    )
    membership = base.membership()
    entry_items: dict[str, uuid.UUID] = {}
    counts = {"added": 0, "superseded": 0, "resolved": 0, "retracted": 0, "dropped": 0}
    applied: list[str] = []

    def supersede(target: dict[str, Any], by: uuid.UUID) -> None:
        tid = (
            uuid.UUID(target["item_id"])
            if "item_id" in target
            else entry_items.get(target.get("entry", ""))
        )
        if tid is not None and tid in membership:
            membership[tid] = ("superseded", by)
            counts["superseded"] += 1

    for e in entries:
        if not accepted(e):
            continue
        action = e["action"]
        if action == "drop_duplicate":
            t = e["target"]
            if "item_id" in t and uuid.UUID(t["item_id"]) in base.items:
                snap = base.items[uuid.UUID(t["item_id"])]
                await update_provenance(
                    session, snap.id, merge_provenance(snap.provenance, e["item"]["provenance"])
                )
            counts["dropped"] += 1
            applied.append(e["id"])
            continue
        if action not in ITEM_ACTIONS:
            continue
        choice = (
            choices.get(e["id"], e.get("default_choice", "keep_both")) if action == "flag" else None
        )
        if choice == "drop":
            counts["dropped"] += 1
            applied.append(e["id"])
            continue
        # item_key is only pre-set for refinements (a new version of an existing key).
        item = NewItem.from_json(e["item"])
        iid, _key = await insert_item(session, repo_id=repo_id, commit_id=commit_id, item=item)
        entry_items[e["id"]] = iid
        membership[iid] = ("active", None)
        counts["added"] += 1
        if action in ("supersede", "refine") or choice == "supersede":
            supersede(e["target"], iid)
        applied.append(e["id"])

    for e in entries:
        if not accepted(e):
            continue
        if e["action"] == "resolve":
            answer = entry_items.get(e["item_entry"])
            tid = uuid.UUID(e["target"]["item_id"])
            if answer is not None and membership.get(tid, ("",))[0] == "active":
                membership[tid] = ("resolved", answer)
                counts["resolved"] += 1
                applied.append(e["id"])
        elif e["action"] == "status":
            tid = uuid.UUID(e["target"]["item_id"])
            if membership.get(tid, ("",))[0] == "active":
                membership[tid] = (e["status"], None)
                counts[e["status"]] += 1
                applied.append(e["id"])

    if counts == {"added": 0, "superseded": 0, "resolved": 0, "retracted": 0, "dropped": 0}:
        raise ValidationFailed("nothing to commit: no entries were accepted")

    await graph.write_membership(session, commit_id, membership)
    events = audit + [
        {
            "stage": "confirm",
            "decision": "applied",
            "details": {"applied": applied, "choices": choices, "counts": counts},
        }
    ]
    await session.execute(
        text(
            "INSERT INTO pipeline_audit (commit_id, stage, decision, details) "
            "SELECT :c, x.stage, x.decision, CAST(x.details AS jsonb) "
            "FROM unnest(CAST(:st AS text[]), CAST(:de AS text[]), CAST(:dt AS text[])) "
            "AS x(stage, decision, details)"
        ),
        {
            "c": commit_id,
            "st": [ev["stage"] for ev in events],
            "de": [ev["decision"] for ev in events],
            "dt": [_json(ev["details"]) for ev in events],
        },
    )
    await graph.move_branch(session, repo_id, branch, commit_id, expected_head=base.commit_id)
    return commit_id, counts


def _json(d: Any) -> str:
    return json.dumps(d, default=str)
