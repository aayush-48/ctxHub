"""Render the active state at a commit for one target model, within a token budget."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config import Settings
from ..errors import ValidationFailed
from ..providers.tokenizers import TokenCounter, target_family
from .items import importance
from .snapshot import SnapItem, Snapshot, cosine
from .text import truncate_words

SECTIONS = [
    ("constraint", "constraints", "Constraints"),
    ("goal", "goals", "Goals"),
    ("conflict", "unresolved_conflicts", "Unresolved conflicts"),
    ("decision", "decisions", "Decisions"),
    ("open_question", "open_questions", "Open questions"),
    ("task", "tasks", "Tasks"),
    ("fact", "facts", "Facts"),
]
# Fill order: constraints and goals, then conflict warnings, then the rest.
TIER = {
    "constraint": 0,
    "goal": 0,
    "conflict": 1,
    "decision": 2,
    "open_question": 3,
    "task": 4,
    "fact": 5,
}
PROTECTED = {"constraint"}

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent.parent / "templates" / "render"),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _flat(s: str) -> str:
    return " ".join(s.split())


@dataclass
class _Line:
    kind: str  # item type or "conflict"
    importance: float
    full: str
    short: str | None
    item: SnapItem | None = None
    form: str = "full"  # full, compressed

    @property
    def protected(self) -> bool:
        """Constraints and pinned items are compressed if needed but never silently dropped."""
        return self.kind in PROTECTED or bool(self.item and self.item.pinned)


@dataclass
class RenderResult:
    text: str
    manifest: dict[str, Any]


def _template_name(template: str, model: str) -> str:
    if template != "auto":
        return template
    fam = target_family(model)
    return {"claude": "claude", "gpt": "gpt"}.get(fam, "plain")


def _item_lines(settings: Settings, snap: Snapshot, ids: bool = True) -> list[_Line]:
    now = datetime.now(UTC)
    replaced_by: dict[Any, list[SnapItem]] = {}
    for i in snap.items.values():
        if i.status == "superseded" and i.superseded_by:
            replaced_by.setdefault(i.superseded_by, []).append(i)
    lines = []
    for i in snap.active():
        age = (now - i.created_at).total_seconds() / 86400
        imp = importance(
            settings,
            i.type,
            age_days=age,
            use_count=i.use_count,
            pinned=i.pinned,
        )
        tag = f" [id:{i.item_key.hex[:8]}]" if ids else ""
        full = _flat(i.content)
        reason = i.attributes.get("reason")
        if reason:
            full += f" Reason: {_flat(reason)}"
        short = _flat(i.compressed) if i.compressed and _flat(i.compressed) != full else None
        if short and reason and i.type != "decision":  # decisions' short form has the reason
            short += f" — {truncate_words(_flat(reason), 100)}"
        olds = replaced_by.get(i.id, [])
        if olds:
            # What this replaced is part of the state (it answers "what did we have before?"),
            # so it survives shortening, in a few words.
            old = olds[-1]
            full += f" (replaces: {_flat(old.compressed or old.content)})"
            if short:
                short += f" (was: {truncate_words(_flat(old.compressed or old.content), 60)})"
        # The subject leads each line: a model saving later changes reuses it, so the server
        # can match the change to this item without the model looking anything up.
        label = f"{_flat(i.subject)}: "
        lines.append(
            _Line(
                kind=i.type,
                importance=imp,
                full=label + full + tag,
                short=(label + short + tag) if short else None,
                item=i,
            )
        )
    return lines


_ALWAYS = {"constraint", "goal", "conflict", "open_question"}
_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "our", "is", "we"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOP}


def _select(
    settings: Settings, lines: list[_Line], focus: str | None, focus_vec: Any
) -> tuple[list[_Line], list[_Line]]:
    """Order a focused or where-you-left-off pull, most important first: what always matters
    (constraints, goals, conflicts, open questions, pinned items), the latest changes, then
    the items that match the focus. Everything else is not loaded."""
    always = [ln for ln in lines if ln.kind in _ALWAYS or (ln.item is not None and ln.item.pinned)]
    rest = [ln for ln in lines if ln not in always]
    recent = sorted(
        (ln for ln in rest if ln.item is not None),
        key=lambda ln: ln.item.created_at,  # type: ignore[union-attr]
        reverse=True,
    )[: settings.pull_recent_items]
    matched: list[_Line] = []
    if focus:
        want = _words(focus)
        fv = np.asarray(focus_vec) if focus_vec is not None else None
        scored = []
        for ln in rest:
            if ln.item is None or ln in recent:
                continue
            it = ln.item
            hits = len(want & _words(f"{it.subject} {it.content}")) / max(len(want), 1)
            sem = (
                max(cosine(fv, it.subject_embedding), cosine(fv, it.embedding))
                if fv is not None
                else 0.0
            )
            if hits + sem >= 0.6:
                scored.append((hits + sem, ln))
        scored.sort(key=lambda x: x[0], reverse=True)
        matched = [ln for _, ln in scored[: settings.pull_focus_items]]
    chosen = always + recent + matched
    picked = set(map(id, chosen))
    return chosen, [ln for ln in lines if id(ln) not in picked]


def _area(subject: str) -> str:
    """The area an item belongs to: its subject without the last word ("Amber Falcon database"
    -> "Amber Falcon"), or the subject itself when it is short."""
    words = subject.split()
    return " ".join(words[:-1]) if len(words) >= 3 else subject


def areas(lines: list[_Line], limit: int = 12) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for ln in lines:
        if ln.item is not None:
            a = _area(ln.item.subject)
            counts[a] = counts.get(a, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"label": a, "items": n} for a, n in top if n > 1][:limit]


def _index_text(not_loaded: list[_Line]) -> str:
    items = [ln for ln in not_loaded if ln.item is not None]
    kinds: dict[str, int] = {}
    for ln in items:
        kinds[ln.kind] = kinds.get(ln.kind, 0) + 1
    by_kind = ", ".join(f"{n} {k.replace('_', ' ')}s" for k, n in sorted(kinds.items()))
    top = areas(items, 8)
    where = "; areas: " + ", ".join(f"{a['label']} ({a['items']})" for a in top) if top else ""
    return (
        f"Also stored but not loaded: {len(items)} items ({by_kind}){where}. When the user asks "
        "about any of them, call ctx_stored with the topic before answering; do not answer "
        "that it is unknown."
    )


async def render(
    settings: Settings,
    counter: TokenCounter,
    snap: Snapshot,
    *,
    repo: str,
    branch: str | None,
    model: str,
    budget: int | None,
    template: str = "auto",
    allow_drop_constraints: bool = False,
    conflicts: list[str] | None = None,
    ids: bool = True,
    select: str = "auto",
    focus: str | None = None,
    focus_vec: Any = None,
) -> RenderResult:
    """`select`: "all" packs every item into the budget; "starter" loads what always matters
    plus the latest changes ("where you left off"); "focus" adds the items that match `focus`.
    In those two, items not loaded are summarized in an index instead of listed."""
    tmpl_name = _template_name(template, model)
    family = target_family(model)  # tokens are counted for the target model, whatever template
    tmpl = _env.get_template(f"{tmpl_name}.j2")
    esc = _xml_escape if tmpl_name == "claude" else (lambda s: s)

    lines = _item_lines(settings, snap, ids)
    for c in conflicts or []:
        lines.append(_Line(kind="conflict", importance=1.0, full=_flat(c), short=None))
    lines.sort(key=lambda ln: (TIER[ln.kind], -ln.importance))
    all_lines = list(lines)
    not_loaded: list[_Line] = []
    index: str | None = None

    def build(selected: list[_Line], dropped: list[_Line]) -> str:
        sections = []
        for kind, tag, title in SECTIONS:
            rows = [
                esc(ln.full if ln.form == "full" else (ln.short or ln.full))
                for ln in selected
                if ln.kind == kind
            ]
            if rows:
                sections.append({"tag": tag, "title": title, "rows": rows})
        omitted = None
        if dropped:
            kinds = sorted({ln.kind.replace("_", " ") for ln in dropped})
            omitted = {
                "count": len(dropped),
                "what": "lowest-importance " + ", ".join(k + "s" for k in kinds),
            }
        return (
            tmpl.render(
                repo=esc(repo),
                branch=esc(branch or "(detached)"),
                commit=snap.commit_id.hex[:8],
                sections=sections,
                omitted=omitted,
                index=esc(index) if index else None,
            ).strip()
            + "\n"
        )

    header_cost = counter.estimate(build([], []), family)
    margin = 1 - settings.render_safety_margin

    def cost(text: str) -> int:
        return counter.estimate(esc(text), family) + 4

    def budget_for(total: int) -> int:
        # The smallest budget whose packing limit fits `total` item tokens after the header.
        return math.ceil((header_cost + total) / margin) + 1

    # Sizes always describe the whole repo, so the view can offer "everything" at its real cost.
    all_full = [cost(ln.full) for ln in all_lines]
    all_min = [cost(ln.short) if ln.short else c for ln, c in zip(all_lines, all_full, strict=True)]
    sizes = {
        # Constraints and pinned items, shortened: below this a render would lose them.
        "minimum": budget_for(
            sum(c for ln, c in zip(all_lines, all_min, strict=True) if ln.protected)
        ),
        # Every item, shortened where it has a short form: nothing is dropped.
        "recommended": budget_for(sum(all_min)),
        # Every item in full: a larger budget adds nothing.
        "full": budget_for(sum(all_full)),
    }
    if select == "auto":
        # A small repo loads whole; a large one loads "where you left off" unless a size is
        # asked for (the size is the user's choice, including everything).
        large = sizes["recommended"] > settings.large_repo_tokens
        select = "starter" if large and budget is None else "all"
    if select in ("starter", "focus"):
        lines, not_loaded = _select(
            settings, lines, focus if select == "focus" else None, focus_vec
        )
        index = _index_text(not_loaded) if not_loaded else None
        header_cost = counter.estimate(build([], []), family)  # now including the index line
    full_costs = [cost(ln.full) for ln in lines]
    min_costs = [cost(ln.short) if ln.short else c for ln, c in zip(lines, full_costs, strict=True)]
    if select != "all":
        sizes["selection"] = budget_for(sum(min_costs))
    if budget is None:
        budget = (
            sizes["recommended"]
            if select == "all"
            else min(sizes["selection"], settings.focus_pull_budget)
        )

    # Pass 1, in priority order: give every item its shortest form while it fits, so items are
    # shortened before anything is dropped. Pass 2: spend what is left on full forms, again in
    # priority order.
    remaining = int(budget * margin) - header_cost
    selected: list[_Line] = []
    dropped: list[_Line] = []
    protected_needed = 0
    for ln, c in zip(lines, min_costs, strict=True):
        if c <= remaining:
            ln.form = "compressed" if ln.short else "full"
            selected.append(ln)
            remaining -= c
        elif ln.protected and not allow_drop_constraints:
            protected_needed += c
        else:
            dropped.append(ln)
    for ln in selected:
        if ln.form == "compressed":
            extra = cost(ln.full) - cost(ln.short or ln.full)
            if extra <= remaining:
                ln.form = "full"
                remaining -= extra
    # Keep render order stable for the template (priority order).
    selected.sort(key=lambda ln: (TIER[ln.kind], -ln.importance))

    if protected_needed:
        raise ValidationFailed(
            "token budget too small to keep every constraint and pinned item; raise the budget, "
            "unpin items, or pass allow_drop_constraints",
            {
                "budget": budget,
                "minimum_budget": sizes["minimum"],
                "additional_tokens_needed": protected_needed - remaining,
            },
        )

    # Verify with the target tokenizer and trim if the estimate was optimistic.
    text = build(selected, dropped)
    tokens, exact = await counter.exact(text, family)
    while tokens > budget and selected:
        victims = [ln for ln in selected if allow_drop_constraints or not ln.protected]
        if not victims:
            break
        victim = max(victims, key=lambda ln: (TIER[ln.kind], -ln.importance))
        if victim.form == "full" and victim.short:
            victim.form = "compressed"
        else:
            selected.remove(victim)
            dropped.append(victim)
        text = build(selected, dropped)
        tokens, exact = await counter.exact(text, family)

    def ref(ln: _Line) -> dict[str, Any]:
        if ln.item is None:
            return {"type": "conflict_warning", "text": ln.full}
        return {
            "item_key": str(ln.item.item_key),
            "type": ln.kind,
            "subject": ln.item.subject,
            "pinned": ln.item.pinned,
        }

    def item_row(ln: _Line, state: str) -> dict[str, Any]:
        assert ln.item is not None
        return {
            **ref(ln),
            "content": ln.item.content,
            "importance": round(ln.importance, 3),
            "state": state,  # full, compressed, dropped
        }

    manifest = {
        "repo": repo,
        "branch": branch,
        "commit": str(snap.commit_id),
        "target_model": model,
        "template": tmpl_name,
        "tokenizer": counter.name(family),
        "token_count_exact": exact,
        "tokens": tokens,
        "budget": budget,
        "sizes": sizes,
        "large": sizes["recommended"] > settings.large_repo_tokens,
        "mode": select,
        "focus": focus if select == "focus" else None,
        "not_loaded": len(not_loaded),
        # Main areas of the repo, offered as focus options for large repos.
        "areas": areas([ln for ln in all_lines if ln.item is not None]),
        "included": len(selected),
        "compressed": [ref(ln) for ln in selected if ln.form == "compressed"],
        "dropped": [ref(ln) for ln in dropped],
        # Every active item and what happened to it, in render order (for the pull view).
        "items": [item_row(ln, ln.form) for ln in selected if ln.item is not None]
        + [item_row(ln, "dropped") for ln in dropped if ln.item is not None],
        "used_item_ids": [str(ln.item.id) for ln in selected if ln.item is not None],
    }
    return RenderResult(text=text, manifest=manifest)
