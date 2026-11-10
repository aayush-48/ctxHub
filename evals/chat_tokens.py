"""Estimate the tokens a claude.ai conversation used, from a data export.

A chat model re-reads the whole conversation on every turn, so the input for each reply is
everything before it, plus fixed overhead (tool definitions when a connector is on). This
script prints, per conversation: the text of each message, the input re-read for each reply,
the output written, and the totals.

Counts use o200k_base (GPT's tokenizer): within roughly 15% of Claude's own counts, which is
fine for comparing two arms of the same test. Claude's hidden system prompt is not included
(it is the same in every chat).

    # claude.ai: Settings -> Privacy -> Export data; unzip; then:
    uv run python -m evals.chat_tokens conversations.json --match "ClinicSlot"
    uv run python -m evals.chat_tokens conversations.json --match "S2" --tools 0
    uv run python -m evals.chat_tokens conversations.json --match "C2" --tools 1500

    # or count any text file (a summary, a pulled context):
    uv run python -m evals.chat_tokens summary1.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import tiktoken

ENC = tiktoken.get_encoding("o200k_base")


def count(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def message_text(m: dict[str, Any]) -> str:
    """Everything in a message the model reads: text, tool calls and tool results."""
    parts: list[str] = []
    for block in m.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "tool_use":
            parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False))
        elif kind == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                parts += [c.get("text", "") for c in content if isinstance(c, dict)]
            else:
                parts.append(json.dumps(content, ensure_ascii=False, default=str))
    if not parts:
        parts.append(m.get("text") or "")
    for a in m.get("attachments") or []:  # pasted long text arrives as an attachment
        parts.append(a.get("extracted_content") or "")
    return "\n".join(p for p in parts if p)


def thinking_text(m: dict[str, Any]) -> str:
    """Visible thinking. claude.ai shows a summary of the model's thinking, so this is a lower
    bound on the real thinking tokens. Thinking is output; earlier turns' thinking is not
    re-read on later turns, so it is not added to the history."""
    return "\n".join(
        b.get("thinking") or b.get("text") or ""
        for b in m.get("content") or []
        if b.get("type") in ("thinking", "redacted_thinking")
    )


def report(conv: dict[str, Any], tools: int) -> None:
    msgs = conv.get("chat_messages") or []
    print(f"\n=== {conv.get('name') or '(untitled)'}  ({len(msgs)} messages)")
    print(f"{'#':>3} {'who':9} {'msg':>7} {'thinking':>9} {'input re-read':>14}")
    history = total_in = total_out = total_think = 0
    for n, m in enumerate(msgs, 1):
        size = count(message_text(m))
        who = m.get("sender", "?")
        if who == "assistant":
            # This reply was produced after reading everything before it.
            think = count(thinking_text(m))
            reread = history + tools
            total_in += reread
            total_out += size
            total_think += think
            print(f"{n:>3} {who:9} {size:>7} {think or '':>9} {reread:>14}")
        else:
            print(f"{n:>3} {who:9} {size:>7}")
        history += size
    print(f"conversation text: {history} tokens")
    print(
        f"input read over the chat: {total_in}  |  output written: {total_out}  |  "
        f"visible thinking: {total_think} (real thinking is higher)  |  "
        f"total: {total_in + total_out + total_think}  (tool overhead {tools}/reply)"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("file", type=Path, help="conversations.json from the export, or a text file")
    ap.add_argument("--match", default="", help="Only conversations whose name contains this.")
    ap.add_argument(
        "--tools",
        type=int,
        default=0,
        help="Tool-definition tokens per reply (about 1500 with ctxHub on).",
    )
    args = ap.parse_args()

    raw = args.file.read_text(encoding="utf-8")
    if args.file.suffix.lower() != ".json":
        print(f"{args.file.name}: {count(raw)} tokens")
        return
    convs = json.loads(raw)
    picked = [c for c in convs if args.match.lower() in (c.get("name") or "").lower()]
    if not picked:
        sys.exit(f"no conversation name contains {args.match!r}")
    for c in picked:
        report(c, args.tools)


if __name__ == "__main__":
    main()
