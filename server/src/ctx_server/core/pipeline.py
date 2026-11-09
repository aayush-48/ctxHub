"""The cleaning pipeline: turns a commit request into a proposed diff, and applies a confirmed
proposal as a commit.

Stages: validate (pydantic, before this module), normalize, embed, dedupe, classify relations,
supersede, score importance, compress. No language model is called here: relations are judged
by rules, embeddings and a local NLI model (core/relations.py), and anything uncertain is
flagged for the user. Every decision is recorded as an audit event.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..errors import ValidationFailed
from ..providers.embeddings import Embedder
from ..providers.nli import NLI
from ..schemas import ItemIn, StatusUpdateIn
from . import graph
from .items import NewItem, importance, insert_item, merge_provenance, update_provenance
from .relations import ItemView, Pair, Relation, answers_question, classify_pairs
from .snapshot import SnapItem, Snapshot, cosine
from .text import content_key, heuristic_compress, normalize_subject, normalize_text

log = logging.getLogger(__name__)

# Actions whose entry carries a new item.
ITEM_ACTIONS = {"add", "supersede", "refine", "flag", "drop_duplicate"}
ADDITIVE_ACTIONS = {"add", "drop_duplicate"}
# Types that can close an open question.
ANSWER_TYPES = {"decision", "fact"}
# Items are compared with others of the same group: the same thing is often recorded as a
# decision in one session and a fact or goal in the next, and must still supersede or dedupe.
TYPE_GROUP = {
    "goal": "state",
    "constraint": "state",
    "decision": "state",
    "fact": "state",
    "task": "task",
    "open_question": "question",
}


def same_group(a: str, b: str) -> bool:
    return TYPE_GROUP.get(a, a) == TYPE_GROUP.get(b, b)


def same_question(settings: Settings, norm_a: str, emb_a: Any, norm_b: str, emb_b: Any) -> bool:
    """Whether two items answer the same question: identical subjects after normalizing, or
    subjects (embedded on their own) that are nearly the same ("Auth provider" and
    "Authentication provider")."""
    if norm_a == norm_b:
        return True
    return cosine(emb_a, emb_b) >= settings.same_subject_threshold


# Notes about the conversation itself rather than the project: what the assistant explained,
# what the user asked for. Chat models sometimes commit these despite the tool instructions.
_CHAT_META = re.compile(
    r"^\s*(provided|explained|answered|summari[sz]ed|described|listed|gave|shared|clarified|"
    r"walked (the user )?through|helped the user)\b"
    r"|\b(the )?user (wants|wanted|asked|asks|may want|might want|would like|is asking)\b"
    r"|\bin (this|the) (chat|conversation)\b",
    re.IGNORECASE,
)


@dataclass
class PipelineDeps:
    settings: Settings
    embedder: Embedder
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
    subject_embedding: Any = None

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
) -> PipelineResult:
    s = deps.settings
    audit: list[dict[str, Any]] = []

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
        texts = [f"{w.item.subject}\n{w.item.content}" for w in work]
        subjects = sorted({w.item.subject for w in work})
        vecs = await deps.embedder.embed(texts + subjects)
        by_subject = dict(zip(subjects, vecs[len(texts) :], strict=True))
        for w, v in zip(work, vecs[: len(texts)], strict=True):
            w.item.embedding = v
            w.item.subject_embedding = by_subject[w.item.subject]
        event("embed", "ok", count=len(work), provider=deps.embedder.name)

    active = snapshot.active()
    pairs: list[tuple[_Work, _Cand, float, bool]] = []

    # -- Dedupe (identical text) and candidate selection
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

        if _CHAT_META.search(w.item.content):
            w.entry = {
                "action": "flag",
                "relation": "chat_meta",
                "default_choice": "drop",
                "reason": "looks like a note about this conversation (what was explained or "
                "asked), not project state; it is skipped unless you keep it",
            }
            event("validate", "chat_meta", entry=w.eid)
            continue

        wv = np.asarray(w.item.embedding) if w.item.embedding is not None else None
        sv = np.asarray(w.item.subject_embedding) if w.item.subject_embedding is not None else None
        pool: list[_Cand] = [
            _Cand(
                _snap_view(a),
                a.subject_norm,
                a.embedding,
                snap=a,
                subject_embedding=a.subject_embedding,
            )
            for a in active
            if same_group(a.type, w.item.type)
        ]
        pool += [
            _Cand(
                _view(p.item),
                p.item.subject_norm,
                np.asarray(p.item.embedding),
                entry_id=p.eid,
                subject_embedding=np.asarray(p.item.subject_embedding),
            )
            for p in work[:idx]
            if same_group(p.item.type, w.item.type)
            and not (p.entry and p.entry["action"] == "drop_duplicate")
            and not (p.entry and p.entry.get("relation") == "chat_meta")
        ]
        scored = []
        for c in pool:
            sim = cosine(wv, c.embedding)
            same_subject = same_question(
                s, c.subject_norm, c.subject_embedding, w.item.subject_norm, sv
            )
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
            # Compare only items that answer the same question, plus near-identical text.
            if same_subject or sim >= s.dedupe_threshold:
                scored.append((c, sim, same_subject))
        if w.entry is not None:
            continue
        scored.sort(key=lambda x: (x[2], x[1]), reverse=True)
        pairs += [(w, c, sim, same) for c, sim, same in scored[:3]]

    # -- Classify relations: rules, embeddings and local NLI (no language model)
    relations = await classify_pairs(
        [Pair(c.view, _view(w.item), sim, same) for w, c, sim, same in pairs],
        s,
        deps.nli,
    )
    for (w, c, sim, _same), r in zip(pairs, relations, strict=True):
        event(
            "classify",
            r.label,
            entry=w.eid,
            target=c.target(),
            similarity=round(sim, 4),
            confidence=r.confidence,
            refined=r.refined,
            reason=r.reason,
            nli=deps.nli is not None,
        )

    # -- Supersede: turn relations into one primary entry per item
    min_conf = s.min_confidence
    for w in work:
        if w.entry is not None:
            continue
        mine = [
            (r, c, sim) for (ww, c, sim, _same), r in zip(pairs, relations, strict=True) if ww is w
        ]
        if not mine:
            w.entry = {"action": "add", "reason": "no stored item answers the same question"}
            continue
        w.entry = _decide(w, mine, min_conf)
        event("supersede", w.entry["action"], entry=w.eid, relation=w.entry.get("relation"))

    # -- Open questions answered by new decisions or facts
    entries_extra: list[dict[str, Any]] = []
    open_qs = [a for a in active if a.type == "open_question"]
    status_target_ids = {t.id for t in status_targets}
    claimed: set[uuid.UUID] = set()
    for w in work:
        if w.item.type not in ANSWER_TYPES or (w.entry and w.entry["action"] == "drop_duplicate"):
            continue
        wv = np.asarray(w.item.embedding)
        sv = np.asarray(w.item.subject_embedding)
        for q in open_qs:
            if q.id in status_target_ids or q.id in claimed:
                continue
            sim = cosine(wv, q.embedding)
            same = same_question(s, q.subject_norm, q.subject_embedding, w.item.subject_norm, sv)
            conf = answers_question(_snap_view(q), _view(w.item), sim, same)
            if not conf:
                continue
            event("classify", "answers", entry=w.eid, question=str(q.id), confidence=conf)
            if conf >= min_conf:
                claimed.add(q.id)
                entries_extra.append(
                    {
                        "action": "resolve",
                        "item_entry": w.eid,
                        "target": _Cand(_snap_view(q), q.subject_norm, None, snap=q).target(),
                        "relation": "answers",
                        "confidence": conf,
                        "reason": "a decision or fact on the same subject answers the question",
                    }
                )

    # -- Score importance and compress (deterministic rules)
    for w in work:
        if not w.entry or w.entry["action"] == "drop_duplicate":
            continue
        it = w.item
        it.compressed = heuristic_compress(it.type, it.content, it.reason)
        if len(it.compressed) > len(it.content):
            it.compressed = it.content
        it.importance = importance(s, it.type, pinned=it.pinned)
        event("score", "ok", entry=w.eid, importance=round(it.importance, 4))
        event("compress", "heuristic", entry=w.eid)

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
        # Two items of the same commit answer the same question. Their order in the commit is
        # not the order things were said in the chat, so a conflicting pair is the user's call.
        if r.label == "duplicate":
            return {**base, "action": "drop_duplicate"}
        if r.label == "refinement":
            if r.refined == "existing":
                return {**base, "action": "drop_duplicate"}
            return {**base, "action": "supersede"}  # keep the more detailed one
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
        if c.snap.type != w.item.type:
            # An item key keeps one type, so a refinement across types replaces the old item.
            return {**base, "action": "supersede"}
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
        if (action in ("supersede", "refine") or choice == "supersede") and e.get("target"):
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
