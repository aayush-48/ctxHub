"""Rule-based relation classification (no language model), with and without NLI scores."""

from __future__ import annotations

import pytest
from ctx_server.config import get_settings
from ctx_server.core.relations import ItemView, Pair, classify_pairs, decide
from ctx_server.providers.nli import NLIScores

S = get_settings()


def pair(a, b, *, type_a="decision", type_b="decision", sim=0.8, same=True):
    return Pair(
        ItemView(type=type_a, subject="s", content=a),
        ItemView(type=type_b, subject="s", content=b),
        sim,
        same,
    )


def nli(contra=0.0, entail=0.0):
    return NLIScores(contradiction=contra, entailment=entail, neutral=1 - contra - entail)


@pytest.mark.parametrize(
    ("a", "b", "kw", "label", "applied"),
    [
        ("Use Postgres.", "use postgres", {}, "duplicate", True),
        ("Use MySQL.", "Use Postgres.", {}, "contradiction", True),  # new decision replaces
        (
            "Region is asia-south1.",
            "Region is us-central1.",
            {"type_a": "fact", "type_b": "fact"},
            "contradiction",
            True,
        ),  # numbers/identifiers changed
        (
            "Ship a CLI MVP",
            "Ship a CLI MVP before the sprint",
            {"type_a": "goal", "type_b": "goal"},
            "refinement",
            True,
        ),
        ("Deploy to asia-south1.", "Deploy to us-central1 for now.", {}, "temporal_scope", True),
        ("Use Redis.", "Do not use Redis.", {}, "contradiction", True),  # negation, not refinement
        (
            "Pilot users are students.",
            "Pilot users are all internal.",
            {"type_a": "fact", "type_b": "fact"},
            "contradiction",
            False,
        ),  # flagged, not applied
        (
            "Use Redis for caching.",
            "Rate limit is 120 per minute.",
            {"same": False, "sim": 0.2},
            "unrelated",
            True,
        ),
    ],
)
def test_rules_without_nli(a, b, kw, label, applied):
    r = decide(pair(a, b, **kw), S)
    assert r.label == label
    assert (r.confidence >= S.min_confidence) == applied


def test_nli_contradiction_and_paraphrase():
    p = pair("Use Cloud SQL Postgres.", "Store data in PostgreSQL on Cloud SQL.", same=True)
    assert decide(p, S, nli(entail=0.9), nli(entail=0.8)).label == "duplicate"
    p = pair("Region A.", "Region B.", type_a="fact", type_b="fact")
    assert decide(p, S, nli(contra=0.95), nli(contra=0.9)).label == "contradiction"


def test_nli_refinement_direction():
    p = pair(
        "Ship an MVP", "Release a first version with merge support", type_a="goal", type_b="goal"
    )
    r = decide(p, S, old_implies_new=nli(), new_implies_old=nli(entail=0.9))
    assert r.label == "refinement" and r.refined == "new"
    r = decide(p, S, old_implies_new=nli(entail=0.9), new_implies_old=nli())
    assert r.refined == "existing"


async def test_classify_pairs_scores_both_directions():
    seen = []

    class StubNLI:
        async def score(self, pairs):
            seen.extend(pairs)
            return [nli(contra=0.9)] * len(pairs)

    [r] = await classify_pairs([pair("A is 1.", "A is 2.")], S, StubNLI())
    assert r.label == "contradiction"
    assert seen == [("A is 1.", "A is 2."), ("A is 2.", "A is 1.")]
