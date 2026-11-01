"""Process-wide dependencies (providers), built once at startup and replaceable in tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings, get_settings
from .core.pipeline import PipelineDeps
from .providers.arbiter import Arbiter, make_arbiter
from .providers.embeddings import Embedder, make_embedder
from .providers.nli import NLI
from .providers.storage import Blob, make_blob
from .providers.tokenizers import TokenCounter

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    embedder: Embedder
    arbiter: Arbiter
    nli: NLI | None
    counter: TokenCounter
    blob: Blob

    def pipeline(self) -> PipelineDeps:
        return PipelineDeps(self.settings, self.embedder, self.arbiter, self.nli)


_runtime: Runtime | None = None


def build_runtime(settings: Settings | None = None) -> Runtime:
    s = settings or get_settings()
    nli = None
    if s.nli_enabled:
        from .providers.nli import CrossEncoderNLI

        nli = CrossEncoderNLI(s.nli_model)
    return Runtime(
        settings=s,
        embedder=make_embedder(s),
        arbiter=make_arbiter(s),
        nli=nli,
        counter=TokenCounter(s),
        blob=make_blob(s),
    )


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt
