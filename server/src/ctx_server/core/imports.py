"""Parse Claude and ChatGPT data exports and extract task state from one conversation."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from ..errors import ValidationFailed
from ..providers.arbiter import Arbiter, ExtractedItem, ItemView
from ..schemas import ItemIn
from .items import TYPE_WEIGHT

MAX_UNZIPPED = 300 * 1024 * 1024
CHUNK_CHARS = 40_000


@dataclass
class Conversation:
    id: str
    title: str
    messages: list[tuple[str, str]]  # (role, text)


def _load_json(data: bytes, filename: str | None) -> Any:
    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise ValidationFailed("upload is not a valid zip file") from e
        names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "conversations.json"]
        if not names:
            raise ValidationFailed("zip has no conversations.json; upload the full data export")
        info = zf.getinfo(names[0])
        if info.file_size > MAX_UNZIPPED:
            raise ValidationFailed("conversations.json is too large")
        with zf.open(info) as f:
            raw = f.read(MAX_UNZIPPED + 1)
    else:
        raw = data
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValidationFailed(f"could not parse {filename or 'upload'} as JSON") from e


def _claude_text(msg: dict[str, Any]) -> str:
    parts = []
    for c in msg.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
    return "\n".join(parts) or (msg.get("text") or "")


def _parse_claude(convs: list[dict[str, Any]]) -> list[Conversation]:
    out = []
    for c in convs:
        msgs = []
        for m in c.get("chat_messages") or []:
            role = "user" if m.get("sender") == "human" else "assistant"
            t = _claude_text(m).strip()
            if t:
                msgs.append((role, t))
        out.append(
            Conversation(
                id=str(c.get("uuid", "")), title=c.get("name") or "(untitled)", messages=msgs
            )
        )
    return out


def _parse_chatgpt(convs: list[dict[str, Any]]) -> list[Conversation]:
    out = []
    for c in convs:
        mapping: dict[str, Any] = c.get("mapping") or {}
        # Follow the active branch: walk parents back from current_node.
        node_id = c.get("current_node")
        chain = []
        seen = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            chain.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
        msgs = []
        for node in reversed(chain):
            m = node.get("message") or {}
            role = (m.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content") or {}
            parts = [p for p in content.get("parts") or [] if isinstance(p, str)]
            t = "\n".join(parts).strip()
            if t:
                msgs.append((role, t))
        out.append(
            Conversation(
                id=str(c.get("conversation_id") or c.get("id") or ""),
                title=c.get("title") or "(untitled)",
                messages=msgs,
            )
        )
    return out


def parse_export(data: bytes, filename: str | None = None) -> tuple[str, list[Conversation]]:
    doc = _load_json(data, filename)
    convs = doc if isinstance(doc, list) else [doc]
    if not convs or not isinstance(convs[0], dict):
        raise ValidationFailed("no conversations found in the export")
    first = convs[0]
    if "chat_messages" in first:
        return "claude_export", _parse_claude(convs)
    if "mapping" in first:
        return "chatgpt_export", _parse_chatgpt(convs)
    raise ValidationFailed("unrecognized export format (expected a Claude or ChatGPT export)")


def select_conversation(convs: list[Conversation], selector: str | None) -> Conversation:
    if selector:
        exact = [c for c in convs if c.id == selector]
        if exact:
            return exact[0]
        matches = [c for c in convs if selector.casefold() in c.title.casefold()]
    else:
        matches = convs
    if len(matches) == 1:
        return matches[0]
    listing = [{"id": c.id, "title": c.title, "messages": len(c.messages)} for c in matches[:50]]
    if not matches:
        raise ValidationFailed(f"no conversation matches {selector!r}")
    raise ValidationFailed(
        "the export has several conversations; choose one with conversation=<id or title>",
        {"conversations": listing, "total": len(matches)},
    )


def transcript_chunks(conv: Conversation, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    chunks, cur, size = [], [], 0
    for role, t in conv.messages:
        line = f"{'User' if role == 'user' else 'Assistant'}: {t}"
        if size + len(line) > chunk_chars and cur:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(line[:chunk_chars])
        size += len(line)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


async def extract_items(arbiter: Arbiter, conv: Conversation, max_items: int) -> list[ItemIn]:
    """Run extraction chunk by chunk, carrying forward what is already known. Later chunks win
    on the same subject, since conversations change their minds."""
    by_subject: dict[tuple[str, str], ExtractedItem] = {}
    for chunk in transcript_chunks(conv):
        known = [
            ItemView(type=i.type, subject=i.subject, content=i.content) for i in by_subject.values()
        ]
        for it in await arbiter.extract(chunk, known):
            by_subject[(it.type, it.subject.casefold())] = it
    # Only answered questions and finished tasks can be closed; models sometimes mark settled
    # decisions or facts "resolved", which must not drop them.
    live = [
        i
        for i in by_subject.values()
        if i.status == "active" or i.type not in ("open_question", "task")
    ]
    live.sort(key=lambda i: -TYPE_WEIGHT.get(i.type, 0))
    out = []
    for i in live[:max_items]:
        try:
            out.append(
                ItemIn(
                    type=i.type,
                    subject=i.subject[:200],
                    content=i.content[:4000],
                    reason=(i.reason or None) and i.reason[:500],
                )
            )
        except Exception:  # skip items the schema rejects (e.g. empty after stripping)
            continue
    return out
