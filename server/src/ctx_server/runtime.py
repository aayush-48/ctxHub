"""Process-wide dependencies (providers), built once at startup and replaceable in tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings, get_settings
from .core.pipeline import PipelineDeps
from .providers.embeddings import Embedder, make_embedder
from .providers.nli import NLI
from .providers.tokenizers import TokenCounter

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    embedder: Embedder
    nli: NLI | None
    counter: TokenCounter

    def pipeline(self) -> PipelineDeps:
        return PipelineDeps(self.settings, self.embedder, self.nli)


_runtime: Runtime | None = None


def build_runtime(settings: Settings | None = None) -> Runtime:
    s = settings or get_settings()
    nli = None
    if s.nli_enabled:
        from .providers.nli import OnnxNLI

        nli = OnnxNLI(s)
    return Runtime(settings=s, embedder=make_embedder(s), nli=nli, counter=TokenCounter(s))


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt
