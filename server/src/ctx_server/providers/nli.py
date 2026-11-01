"""Optional NLI cross-encoder used as a cheap prefilter before the LLM arbiter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass
class NLIScores:
    contradiction: float
    entailment: float
    neutral: float


class NLI(Protocol):
    async def score(self, pairs: list[tuple[str, str]]) -> list[NLIScores]: ...


class CrossEncoderNLI:
    def __init__(self, model: str):
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(model, device="cpu")
        # Map label names to output columns from the model config.
        id2label = self._model.model.config.id2label
        self._cols = {v.lower(): int(k) for k, v in id2label.items()}

    async def score(self, pairs: list[tuple[str, str]]) -> list[NLIScores]:
        if not pairs:
            return []
        probs = await asyncio.to_thread(
            self._model.predict, pairs, apply_softmax=True, convert_to_numpy=True
        )
        return [
            NLIScores(
                contradiction=float(p[self._cols["contradiction"]]),
                entailment=float(p[self._cols["entailment"]]),
                neutral=float(p[self._cols["neutral"]]),
            )
            for p in probs
        ]
