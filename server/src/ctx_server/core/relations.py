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
    similarity: float
    same_subject: bool


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


def material_difference(a: str, b: str) -> bool:
    """Texts that differ in numbers/identifiers or in negation say different things, however
    similar their wording."""
    da = set(_DIGIT_TOKEN.findall(a.lower()))
    db = set(_DIGIT_TOKEN.findall(b.lower()))
    return da != db or bool(_NEGATION.search(a)) != bool(_NEGATION.search(b))


def decide(
    p: Pair,
    settings: Settings,
    old_implies_new: NLIScores | None = None,
    new_implies_old: NLIScores | None = None,
) -> Relation:
    a, b = p.existing, p.new
    related = p.same_subject or p.similarity >= settings.related_threshold
    ka, kb = content_key(a.content), content_key(b.content)

    if ka == kb:
        return Relation(label="duplicate", confidence=1.0, reason="identical content")
    negation_flip = bool(_NEGATION.search(a.content)) != bool(_NEGATION.search(b.content))
    if related and (ka in kb or kb in ka) and not negation_flip:
        # One statement is the other plus detail ("Ship an MVP" -> "Ship an MVP this sprint").
        longer = "new" if len(kb) > len(ka) else "existing"
        return Relation(
            label="refinement", confidence=0.8, refined=longer, reason="one extends the other"
        )
    if related and (_TEMPORAL.search(a.content) or _TEMPORAL.search(b.content)):
        return Relation(
            label="temporal_scope",
            confidence=0.8,
            reason="one of them only holds for a period or condition",
        )
    if old_implies_new and new_implies_old:
        contra = max(old_implies_new.contradiction, new_implies_old.contradiction)
        e_old, e_new = old_implies_new.entailment, new_implies_old.entailment
        if contra >= NLI_CONTRADICTION and related:
            return Relation(
                label="contradiction", confidence=round(contra, 3), reason="NLI: contradicts"
            )
        if e_old >= NLI_ENTAILMENT and e_new >= NLI_ENTAILMENT:
            return Relation(
                label="duplicate",
                confidence=round(min(e_old, e_new), 3),
                reason="NLI: each implies the other",
            )
        if e_new >= NLI_REFINEMENT and related:
            return Relation(
                label="refinement",
                confidence=round(e_new, 3),
                refined="new",
                reason="NLI: the new item implies the old one and adds detail",
            )
        if e_old >= NLI_REFINEMENT and related:
            return Relation(
                label="refinement",
                confidence=round(e_old, 3),
                refined="existing",
                reason="NLI: the existing item already implies the new one",
            )
    if related and material_difference(a.content, b.content):
        return Relation(
            label="contradiction",
            confidence=0.85,
            reason="same subject with different numbers, names or negation",
        )
    if p.similarity >= settings.dedupe_threshold:
        return Relation(
            label="duplicate",
            confidence=round(p.similarity, 3),
            reason="near-identical wording",
        )
    if p.same_subject:
        if a.type in SINGLE_VALUED and b.type in SINGLE_VALUED:
            return Relation(
                label="contradiction",
                confidence=0.75,
                reason="a new decision on the same subject replaces the old one",
            )
        # Facts, goals and constraints can legitimately coexist on one subject; let the
        # user decide rather than guess.
        return Relation(
            label="contradiction",
            confidence=0.6,
            reason="same subject, different content; check whether it replaces the old item",
        )
    if p.similarity >= settings.related_threshold:
        return Relation(label="complementary", confidence=0.6, reason="related but compatible")
    return Relation(label="unrelated", confidence=0.9)


async def classify_pairs(pairs: list[Pair], settings: Settings, nli: NLI | None) -> list[Relation]:
    """Classify each pair, running NLI in both directions for all pairs in one batch."""
    if not pairs:
        return []
    if nli is None:
        return [decide(p, settings) for p in pairs]
    texts = [(p.existing.content, p.new.content) for p in pairs]
    scores = await nli.score(texts + [(b, a) for a, b in texts])
    n = len(pairs)
    return [decide(p, settings, scores[i], scores[n + i]) for i, p in enumerate(pairs)]


def answers_question(
    question: ItemView, candidate: ItemView, similarity: float, same_subject: bool
) -> float:
    """Confidence that a decision or fact answers an open question (0 when it does not)."""
    if same_subject:
        return 0.8
    if similarity >= 0.85:
        return 0.65  # below the default minimum: not applied automatically
    return 0.0
