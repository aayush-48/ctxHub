"""Render the active state at a commit for one target model, within a token budget."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config import Settings
from ..errors import ValidationFailed
from ..providers.tokenizers import TokenCounter, target_family
from .items import importance
from .snapshot import SnapItem, Snapshot

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


def _item_lines(settings: Settings, snap: Snapshot) -> list[_Line]:
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
        tag = f" [id:{i.item_key.hex[:8]}]"
        full = _flat(i.content)
        reason = i.attributes.get("reason")
        if reason and i.type == "decision":
            full += f" Reason: {_flat(reason)}"
        olds = replaced_by.get(i.id, [])
        if olds:
            old = olds[-1]
            full += f" (replaces: {_flat(old.compressed or old.content)})"
        short = _flat(i.compressed) if i.compressed and _flat(i.compressed) != full else None
        lines.append(
            _Line(
                kind=i.type,
                importance=imp,
                full=full + tag,
                short=(short + tag) if short else None,
                item=i,
            )
        )
    return lines


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
) -> RenderResult:
    tmpl_name = _template_name(template, model)
    family = target_family(model)  # tokens are counted for the target model, whatever template
    tmpl = _env.get_template(f"{tmpl_name}.j2")
    esc = _xml_escape if tmpl_name == "claude" else (lambda s: s)

    lines = _item_lines(settings, snap)
    for c in conflicts or []:
        lines.append(_Line(kind="conflict", importance=1.0, full=_flat(c), short=None))
    lines.sort(key=lambda ln: (TIER[ln.kind], -ln.importance))

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
            ).strip()
            + "\n"
        )

    header_cost = counter.estimate(build([], []), family)
    margin = 1 - settings.render_safety_margin

    def cost(text: str) -> int:
        return counter.estimate(esc(text), family) + 4

    full_costs = [cost(ln.full) for ln in lines]
    min_costs = [cost(ln.short) if ln.short else c for ln, c in zip(lines, full_costs, strict=True)]

    def budget_for(total: int) -> int:
        # The smallest budget whose packing limit fits `total` item tokens after the header.
        return math.ceil((header_cost + total) / margin) + 1

    sizes = {
        # Constraints and pinned items, shortened: below this a render would lose them.
        "minimum": budget_for(
            sum(c for ln, c in zip(lines, min_costs, strict=True) if ln.protected)
        ),
        # Every item, shortened where it has a short form: nothing is dropped.
        "recommended": budget_for(sum(min_costs)),
        # Every item in full: a larger budget adds nothing.
        "full": budget_for(sum(full_costs)),
    }
    if budget is None:
        budget = sizes["recommended"]

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
        "included": len(selected),
        "compressed": [ref(ln) for ln in selected if ln.form == "compressed"],
        "dropped": [ref(ln) for ln in dropped],
        # Every active item and what happened to it, in render order (for the pull view).
        "items": [item_row(ln, ln.form) for ln in selected if ln.item is not None]
        + [item_row(ln, "dropped") for ln in dropped if ln.item is not None],
        "used_item_ids": [str(ln.item.id) for ln in selected if ln.item is not None],
    }
    return RenderResult(text=text, manifest=manifest)
