"""The real local NLI model through the rules. Downloads the model (~170 MB) on first run, so
it only runs when CTX_TEST_NLI=1."""

from __future__ import annotations

import os

import pytest
from ctx_server.config import Settings
from ctx_server.core.relations import ItemView, Pair, classify_pairs

pytestmark = pytest.mark.skipif(os.environ.get("CTX_TEST_NLI") != "1", reason="set CTX_TEST_NLI=1")


@pytest.fixture(scope="module")
def nli():
    from ctx_server.providers.nli import OnnxNLI

    return OnnxNLI(Settings(nli_enabled=True))


def p(type_, a, b, same=True, sim=0.8):
    return Pair(
        ItemView(type=type_, subject="s", content=a),
        ItemView(type=type_, subject="s", content=b),
        sim,
        same,
    )


async def test_real_nli_judgments(nli):
    s = Settings()
    pairs = [
        p("fact", "The service runs in asia-south1.", "The service runs in us-central1."),
        p(
            "constraint",
            "Must run on GCP; no paid vector DB.",
            "Everything has to run on Google Cloud, and we can't pay for a vector database.",
        ),
        p("decision", "Use MongoDB.", "Use Postgres."),
        # A different choice that happens to imply the old one in general terms.
        p("decision", "Use a separate vector DB.", "Use Cloud SQL Postgres with pgvector."),
    ]
    labels = [r.label for r in await classify_pairs(pairs, s, nli)]
    assert labels == ["contradiction", "duplicate", "contradiction", "contradiction"]
