"""Embedding providers. Every provider returns unit-normalized vectors of EMBEDDING_DIM."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Protocol

import numpy as np

from ..config import EMBEDDING_DIM, Settings
from ..errors import Unavailable


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _unit(v: np.ndarray) -> list[float]:
    n = float(np.linalg.norm(v))
    return (v / n).tolist() if n > 0 else v.tolist()


class FakeEmbedder:
    """Deterministic hashed bag of words and bigrams. Similar texts get similar vectors,
    which is enough to exercise dedupe and candidate selection in tests."""

    name = "fake"
    _TOKEN = re.compile(r"\w+")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            words = self._TOKEN.findall(t.casefold())
            feats = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
            v = np.zeros(EMBEDDING_DIM)
            for f in feats:
                h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
                v[h % EMBEDDING_DIM] += 1.0 if (h >> 32) & 1 else -1.0
            out.append(_unit(v))
        return out


class GenAIEmbedder:
    """Gemini embeddings through Vertex AI (service-account auth) or an AI Studio API key."""

    def __init__(self, settings: Settings, vertex: bool):
        from google import genai

        if vertex:
            self._client = genai.Client(
                vertexai=True, project=settings.gcp_project, location=settings.vertex_location
            )
        else:
            if not settings.gemini_api_key:
                raise RuntimeError(
                    "CTX_GEMINI_API_KEY is required for embedding_provider=gemini_api"
                )
            self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.embedding_model
        self.name = f"{'vertex' if vertex else 'gemini_api'}:{self._model}"
        self._sem = asyncio.Semaphore(8)

    async def _one(self, text: str) -> list[float]:
        from google.genai import types

        async with self._sem:
            resp = await self._client.aio.models.embed_content(
                model=self._model,
                contents=text,
                config=types.EmbedContentConfig(
                    task_type="SEMANTIC_SIMILARITY", output_dimensionality=EMBEDDING_DIM
                ),
            )
        assert resp.embeddings and resp.embeddings[0].values
        return _unit(np.asarray(resp.embeddings[0].values, dtype=np.float64))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # One text per request: some Gemini embedding models accept only a single input.
        try:
            return list(await asyncio.gather(*(self._one(t) for t in texts)))
        except Exception as e:  # network, quota, auth
            raise Unavailable(f"embedding service failed: {e}") from e


class LocalEmbedder:
    """sentence-transformers on CPU (requires the local-embeddings extra)."""

    def __init__(self, model: str = "BAAI/bge-base-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model, device="cpu")
        dim = self._model.get_sentence_embedding_dimension()
        if dim != EMBEDDING_DIM:
            raise RuntimeError(f"local embedding model has dim {dim}, schema needs {EMBEDDING_DIM}")
        self.name = f"local:{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = await asyncio.to_thread(self._model.encode, texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


def make_embedder(settings: Settings) -> Embedder:
    match settings.embedding_provider:
        case "fake":
            return FakeEmbedder()
        case "vertex":
            return GenAIEmbedder(settings, vertex=True)
        case "gemini_api":
            return GenAIEmbedder(settings, vertex=False)
        case "local":
            model = settings.embedding_model
            return LocalEmbedder(model if "/" in model else "BAAI/bge-base-en-v1.5")
