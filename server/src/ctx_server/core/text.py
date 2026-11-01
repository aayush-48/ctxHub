"""Text normalization and the deterministic compressed form of items."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"[ \t\r\f\v]+")
_NON_WORD = re.compile(r"[^\w\s]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_END = re.compile(r"[;:\n]|,\s|\.\s")


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    lines = [_WS.sub(" ", line).strip() for line in s.strip().splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def normalize_subject(s: str) -> str:
    """Canonical subject used for matching: case-folded, punctuation-free, single-spaced."""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = _NON_WORD.sub(" ", s)
    return " ".join(s.split())


def content_key(s: str) -> str:
    return normalize_subject(s)


def truncate_words(s: str, limit: int) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def first_sentence(s: str) -> str:
    s = " ".join(s.split())
    return _SENTENCE_END.split(s, maxsplit=1)[0]


def first_clause(s: str) -> str:
    s = " ".join(s.split())
    return _CLAUSE_END.split(s, maxsplit=1)[0].rstrip(".")


def heuristic_compress(item_type: str, content: str, reason: str | None = None) -> str:
    """Typed short form: decisions keep the choice plus a one-line reason; constraints and
    open questions stay verbatim; facts compress to one clause."""
    if item_type in ("constraint", "open_question"):
        return content
    if item_type == "decision":
        choice = truncate_words(first_sentence(content), 160)
        if reason:
            return f"{choice} — {truncate_words(first_sentence(reason), 100)}"
        return choice
    if item_type == "fact":
        return truncate_words(first_clause(content), 140)
    return truncate_words(first_sentence(content), 160)
