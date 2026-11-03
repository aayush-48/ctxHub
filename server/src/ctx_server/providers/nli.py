"""Local NLI cross-encoder (no API calls): judges whether one statement entails, contradicts
or is neutral to another. Runs an ONNX build of cross-encoder/nli-deberta-v3-small on CPU,
without PyTorch."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ..config import Settings

MAX_TOKENS = 256
BATCH = 16


@dataclass
class NLIScores:
    contradiction: float
    entailment: float
    neutral: float


class NLI(Protocol):
    async def score(self, pairs: list[tuple[str, str]]) -> list[NLIScores]:
        """Scores for (premise, hypothesis) pairs."""


def model_files(settings: Settings) -> tuple[Path, Path, Path]:
    """(model.onnx, tokenizer.json, config.json): from CTX_NLI_MODEL_DIR when set (baked into
    the container image), otherwise downloaded once from Hugging Face into the local cache."""
    if settings.nli_model_dir:
        d = Path(settings.nli_model_dir)
        return d / "model.onnx", d / "tokenizer.json", d / "config.json"
    from huggingface_hub import hf_hub_download

    repo = settings.nli_model_repo
    return (
        Path(hf_hub_download(repo, settings.nli_model_file)),
        Path(hf_hub_download(repo, "tokenizer.json")),
        Path(hf_hub_download(repo, "config.json")),
    )


class OnnxNLI:
    def __init__(self, settings: Settings):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model, tokenizer, config = model_files(settings)
        self._tok = Tokenizer.from_file(str(tokenizer))
        self._tok.enable_truncation(MAX_TOKENS)
        self._tok.enable_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self._sess = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        id2label = json.loads(config.read_text(encoding="utf-8"))["id2label"]
        self._col = {v.lower(): int(k) for k, v in id2label.items()}

    def _run(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        enc = self._tok.encode_batch(pairs)
        feed = {
            "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
        }
        if "token_type_ids" in self._inputs:
            feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
        logits = self._sess.run(None, feed)[0]
        p = np.exp(logits - logits.max(axis=1, keepdims=True))
        return p / p.sum(axis=1, keepdims=True)

    async def score(self, pairs: list[tuple[str, str]]) -> list[NLIScores]:
        out: list[NLIScores] = []
        for i in range(0, len(pairs), BATCH):
            probs = await asyncio.to_thread(self._run, pairs[i : i + BATCH])
            out += [
                NLIScores(
                    contradiction=float(p[self._col["contradiction"]]),
                    entailment=float(p[self._col["entailment"]]),
                    neutral=float(p[self._col["neutral"]]),
                )
                for p in probs
            ]
        return out
