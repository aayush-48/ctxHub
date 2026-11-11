"""Token budget for what the user's chat model reads from ctx. Every tool definition is sent
with each message and every tool result stays in the chat, so these sizes are product cost.
Counted with o200k (Claude's own counts are within roughly 15%)."""

from __future__ import annotations

import json

from ctx_server import mcp_server, service
from ctx_server.config import get_settings
from ctx_server.providers.tokenizers import _o200k
from ctx_server.schemas import CommitIn, PullIn
from test_same_question import ROAD_HAZARD

# Ceilings, with ~10% headroom over the measured sizes.
TOOL_DEFINITIONS = 1850  # six tools; saving needs no lookup, so the commit rules live here
COMMIT_RESULT_PER_ENTRY = 25  # one line per plain "add"


def tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return len(_o200k().encode(text))


def visible(tool) -> bool:
    meta = tool.model_dump(by_alias=True, exclude_none=True).get("_meta") or {}
    return (meta.get("ui") or {}).get("visibility") != ["app"]


async def test_tool_definitions_fit_the_budget():
    tools = [t for t in await mcp_server.build_mcp(get_settings()).list_tools() if visible(t)]
    sizes = {
        t.name: tokens(
            {
                k: v
                for k, v in t.model_dump(by_alias=True, exclude_none=True).items()
                if k != "_meta"
            }
        )
        for t in tools
    }
    assert sum(sizes.values()) + tokens(mcp_server.INSTRUCTIONS) <= TOOL_DEFINITIONS, sizes


async def test_commit_result_is_compact(alice, repo):
    items = ROAD_HAZARD[:9]  # the project facts (the last two are notes about the chat)
    r = await service.commit(alice, CommitIn(repo=repo, message="m", items=items))
    compact = mcp_server._view(
        "commit", mcp_server._compact_commit(r), {"tool": "ctx_app_proposal", "args": {}}
    )
    assert tokens(compact) <= 80 + COMMIT_RESULT_PER_ENTRY * len(items)
    assert tokens(compact) * 4 < tokens(r), "compact result should be far smaller than the full one"


async def test_chat_pulls_leave_out_item_ids(alice, repo):
    await service.commit(alice, CommitIn(repo=repo, message="m", items=ROAD_HAZARD[:9]))
    with_ids = (await service.pull(alice, repo, PullIn()))["context"]
    without = (await service.pull(alice, repo, PullIn(ids=False)))["context"]
    assert "[id:" in with_ids and "[id:" not in without
    assert tokens(without) < tokens(with_ids)
