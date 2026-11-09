"""Rule-based relation classification between two state items: no language-model calls.

Signals: exact text, embedding similarity, subject match, numbers and negation, wording that
marks a temporary situation, and (when enabled) a local NLI model run in both directions.
The confidence attached to each verdict decides what happens next: confident verdicts are
applied (after the user confirms the diff), and anything below the configured minimum is
flagged for the user instead of guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..providers.nli import NLI, NLIScores
from .text import content_key

RelationLabel = Literal[
    "duplicate", "refinement", "complementary", "contradiction", "temporal_scope", "unrelated"
]


class ItemView(BaseModel):
    type: str
    subject: str
    content: str
    reason: str | None = None


class Relation(BaseModel):
    label: RelationLabel
    confidence: float = Field(ge=0, le=1)
    refined: Literal["existing", "new"] | None = None
    reason: str = ""


@dataclass
class Pair:
    existing: ItemView
    new: ItemView
    similarity: float  # of subject + content embeddings
    same_subject: bool  # the two items answer the same question (see pipeline.same_question)


_TEMPORAL = re.compile(
    r"\b(for now|until|temporar\w*|during|meanwhile|in the meantime|after launch|"
    r"before launch|this (sprint|week|month|quarter)|for the time being|short[- ]term)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(not|no|never|without|don't|doesn't|won't|cannot|can't|isn't|aren't)\b", re.I
)
_DIGIT_TOKEN = re.compile(r"\b\S*\d\S*\b")

# Types that hold a single current answer per subject: a new one on the same subject replaces
# the old one (the design's "new decision on the same subject supersedes the old one").
SINGLE_VALUED = {"decision"}

NLI_CONTRADICTION = 0.5
# Two statements that each imply the other are duplicates.
NLI_ENTAILMENT = 0.6
# One-way implication is weaker evidence: "Postgres with pgvector" implies "a vector DB", yet
# replaces "a separate vector DB". Only very strong one-way implication counts as refinement.
NLI_REFINEMENT = 0.9


def _value_tokens(text: str) -> set[str]:
    return {t.strip(".,;:()") for t in _DIGIT_TOKEN.findall(text)}


def material_difference(a: str, b: str) -> bool:
    """Texts that differ in numbers/identifiers or in negation say different things, however
    similar their wording."""
    return _value_tokens(a.lower()) != _value_tokens(b.lower()) or bool(
        _NEGATION.search(a)
    ) != bool(_NEGATION.search(b))


def _describe_difference(a: str, b: str) -> str:
    old, new = _value_tokens(a) - _value_tokens(b), _value_tokens(b) - _value_tokens(a)
    if old or new:
        return f"different values: {', '.join(sorted(old)[:3]) or '-'} -> {', '.join(sorted(new)[:3]) or '-'}"
    return "one of them is negated"


def word_count(text: str) -> int:
    return len(text.split())


def decide(
    p: Pair,
    settings: Settings,
    old_implies_new: NLIScores | None = None,
    new_implies_old: NLIScores | None = None,
) -> Relation:
    """Classify one pair. The first question is whether the two items answer the same
    question (same subject); only then is their content compared. Items about different
    questions are never contradictions, however similar the project they describe."""
    a, b = p.existing, p.new
    ka, kb = content_key(a.content), content_key(b.content)

    if ka == kb:
        return Relation(label="duplicate", confidence=1.0, reason="identical content")
    if not p.same_subject:
        if p.similarity >= settings.dedupe_threshold and not material_difference(
            a.content, b.content
        ):
            return Relation(
                label="duplicate",
                confidence=round(p.similarity, 3),
                reason=f"near-identical wording ({p.similarity:.0%} similar)",
            )
        return Relation(label="unrelated", confidence=0.9, reason="different subjects")

    on = f"same subject '{b.subject}'"
    negation_flip = bool(_NEGATION.search(a.content)) != bool(_NEGATION.search(b.content))
    if (ka in kb or kb in ka) and not negation_flip:
        # One statement is the other plus detail ("Ship an MVP" -> "Ship an MVP this sprint").
        longer = "new" if len(kb) > len(ka) else "existing"
        return Relation(
            label="refinement",
            confidence=0.8,
            refined=longer,
            reason=f"{on}: one statement extends the other",
        )
    if _TEMPORAL.search(a.content) or _TEMPORAL.search(b.content):
        return Relation(
            label="temporal_scope",
            confidence=0.8,
            reason=f"{on}: one of them only holds for a period or condition",
        )
    if old_implies_new and new_implies_old:
        contra = max(old_implies_new.contradiction, new_implies_old.contradiction)
        e_old, e_new = old_implies_new.entailment, new_implies_old.entailment
        if contra >= NLI_CONTRADICTION:
            return Relation(
                label="contradiction",
                confidence=round(contra, 3),
                reason=f"{on}: the NLI model finds they contradict ({contra:.0%})",
            )
        if e_old >= NLI_ENTAILMENT and e_new >= NLI_ENTAILMENT:
            return Relation(
                label="duplicate",
                confidence=round(min(e_old, e_new), 3),
                reason=f"{on}: each implies the other (NLI)",
            )
        if e_new >= NLI_REFINEMENT:
            return Relation(
                label="refinement",
                confidence=round(e_new, 3),
                refined="new",
                reason=f"{on}: the new item implies the old one and adds detail (NLI)",
            )
        if e_old >= NLI_REFINEMENT:
            return Relation(
                label="refinement",
                confidence=round(e_old, 3),
                refined="existing",
                reason=f"{on}: the stored item already implies the new one (NLI)",
            )
    if material_difference(a.content, b.content):
        return Relation(
            label="contradiction",
            confidence=0.85,
            reason=f"{on}: {_describe_difference(a.content, b.content)}",
        )
    if p.similarity >= settings.dedupe_threshold:
        return Relation(
            label="duplicate",
            confidence=round(p.similarity, 3),
            reason=f"{on}: near-identical wording",
        )
    if a.type in SINGLE_VALUED and b.type in SINGLE_VALUED:
        return Relation(
            label="contradiction",
            confidence=0.75,
            reason=f"{on}: a new decision replaces the old one",
        )
    # Facts, goals and constraints can legitimately coexist on one subject; let the user
    # decide rather than guess.
    return Relation(
        label="contradiction",
        confidence=0.6,
        reason=f"{on}, different content: check whether it replaces the stored item",
    )


async def classify_pairs(pairs: list[Pair], settings: Settings, nli: NLI | None) -> list[Relation]:
    """Classify each pair. NLI runs (both directions, one batch) only where it is reliable:
    pairs about the same question whose statements are both short."""
    if not pairs:
        return []
    use_nli = [
        nli is not None
        and p.same_subject
        and word_count(p.existing.content) <= settings.nli_max_words
        and word_count(p.new.content) <= settings.nli_max_words
        for p in pairs
    ]
    idx = [i for i, u in enumerate(use_nli) if u]
    scores: dict[int, tuple[NLIScores, NLIScores]] = {}
    if idx and nli is not None:
        texts = [(pairs[i].existing.content, pairs[i].new.content) for i in idx]
        out = await nli.score(texts + [(b, a) for a, b in texts])
        scores = {i: (out[n], out[len(idx) + n]) for n, i in enumerate(idx)}
    return [decide(p, settings, *scores.get(i, (None, None))) for i, p in enumerate(pairs)]


def answers_question(
    question: ItemView, candidate: ItemView, similarity: float, same_subject: bool
) -> float:
    """Confidence that a decision or fact answers an open question (0 when it does not)."""
    if same_subject:
        return 0.8
    if similarity >= 0.85:
        return 0.65  # below the default minimum: not applied automatically
    return 0.0
