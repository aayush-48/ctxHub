"""Token counting per target model.

GPT-family targets use tiktoken (o200k_base). Claude targets use Anthropic's token-counting
API when an API key is configured; otherwise an o200k count scaled by a safety factor, and the
render manifest reports the count as an estimate.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

import tiktoken

from ..config import Settings

log = logging.getLogger(__name__)

TargetFamily = Literal["claude", "gpt", "plain"]

CLAUDE_COUNT_MODEL = "claude-haiku-5-5"


def target_family(model: str) -> TargetFamily:
    m = model.lower()
    if m.startswith("claude") or m in ("anthropic",):
        return "claude"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "openai")):
        return "gpt"
    return "plain"


@lru_cache
def _o200k() -> tiktoken.Encoding:
    return tiktoken.get_encoding("o200k_base")


class TokenCounter:
    def __init__(self, settings: Settings):
        self._factor = settings.claude_estimate_factor
        self._anthropic = None
        if settings.anthropic_api_key:
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)

    def name(self, family: TargetFamily) -> str:
        if family == "claude":
            return "anthropic-count-tokens" if self._anthropic else "o200k_base*estimate"
        return "o200k_base"

    def estimate(self, text: str, family: TargetFamily) -> int:
        """Fast local count used while packing; never under-counts by design for Claude."""
        n = len(_o200k().encode(text, disallowed_special=()))
        return int(n * self._factor) + 1 if family == "claude" else n

    async def exact(self, text: str, family: TargetFamily) -> tuple[int, bool]:
        """(count, is_exact). Falls back to the estimate if the API is unavailable."""
        if family == "claude" and self._anthropic is not None:
            try:
                r = await self._anthropic.messages.count_tokens(
                    model=CLAUDE_COUNT_MODEL, messages=[{"role": "user", "content": text}]
                )
                return r.input_tokens, True
            except Exception as e:
                log.warning("anthropic token counting failed, using estimate: %s", e)
                return self.estimate(text, family), False
        return self.estimate(text, family), family != "claude"
