"""Comparison is about the same question (subject), not about the same project: a commit of
many different facts must not raise flags, while real conflicts still do."""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from ctx_server import service
from ctx_server.config import get_settings
from ctx_server.core.pipeline import same_question
from ctx_server.schemas import CommitIn, ConfirmIn
from pydantic import ValidationError


def item(type_, subject, content):
    return {"type": type_, "subject": subject, "content": content}


async def propose(actor, repo, items):
    return await service.commit(
        actor, CommitIn(repo=repo, message="m", items=items, auto_confirm=False)
    )


# The road-hazard chat from user testing, as the commit instructions now ask for it: one fact
# per item, a specific subject each, plus two notes about the chat itself.
ROAD_HAZARD = [
    item(
        "goal",
        "Project scope",
        "Build a road-hazard detection system for Volkswagen cars that alerts nearby drivers.",
    ),
    item("fact", "Hazards detected", "Detects potholes, speed bumps, debris and stalled vehicles."),
    item("fact", "Vehicle sensors", "Uses in-car cameras, GPS, IMU and CAN bus data."),
    item("fact", "Detection model", "Detection uses DEIMv2-S with a DINOv3 backbone."),
    item(
        "fact",
        "Model comparison",
        "DEIMv2-S scores 50.9 mAP(50-95) versus 47.0 for YOLO11-S at 24.3 vs 21.5 GFLOPs.",
    ),
    item("fact", "Tracking", "Hazards are tracked across frames with SORT or StrongSORT."),
    item("fact", "Privacy", "Faces and plates are blurred on-device before any upload."),
    item(
        "fact",
        "Alert delivery",
        "Alerts go to nearby cars over V2X/DSRC and to the cloud over MQTT or HTTPS.",
    ),
    item(
        "fact",
        "Cost per vehicle",
        "OEM-integrated cost is Rs 4,000-9,000 per vehicle per year with no extra hardware.",
    ),
    item(
        "decision",
        "Glossary",
        "Provided a grouped glossary of detection metrics, models and cost terms.",
    ),
    item(
        "open_question",
        "Next explanation",
        "User may want a deeper explanation of how mAP is calculated.",
    ),
]


async def test_many_different_facts_raise_no_flags(alice, repo):
    r = await propose(alice, repo, ROAD_HAZARD)
    flagged = [e for e in r["entries"] if e["action"] == "flag"]
    # Only the two notes about the chat are flagged, pre-set to be skipped.
    assert sorted(e["item"]["subject"] for e in flagged) == ["Glossary", "Next explanation"]
    assert all(e["relation"] == "chat_meta" and e["default_choice"] == "drop" for e in flagged)
    assert {e["action"] for e in r["entries"]} == {"add", "flag"}

    done = await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    assert done["counts"]["added"] == len(ROAD_HAZARD) - 2
    assert done["counts"]["dropped"] == 2


async def test_conflict_inside_one_commit_is_flagged(alice, repo):
    r = await propose(
        alice,
        repo,
        [
            item("decision", "Primary datastore", "Use Postgres."),
            item("decision", "Primary datastore", "Use MongoDB."),
        ],
    )
    first, second = r["entries"]
    assert first["action"] == "add"
    # Order inside a commit says nothing about which came later: the user decides.
    assert second["action"] == "flag" and second["target"]["entry"] == "e1"


async def test_refinement_inside_one_commit_keeps_the_detailed_item(alice, repo):
    r = await propose(
        alice,
        repo,
        [
            item("goal", "MVP", "Ship a CLI MVP"),
            item("goal", "MVP", "Ship a CLI MVP before the next sprint"),
        ],
    )
    assert r["entries"][1]["action"] == "supersede"
    await service.confirm(alice, uuid.UUID(r["proposal_id"]), ConfirmIn())
    items = (await service.list_items(alice, repo, None))["items"]
    assert [i["content"] for i in items] == ["Ship a CLI MVP before the next sprint"]


async def test_conflict_across_commits_replaces(alice, repo):
    await service.commit(
        alice,
        CommitIn(
            repo=repo, message="m", items=[item("decision", "Primary datastore", "Use Postgres.")]
        ),
    )
    r = await propose(alice, repo, [item("decision", "Primary datastore", "Use MongoDB.")])
    [e] = r["entries"]
    assert e["action"] == "supersede"
    assert "Primary datastore" in e["reason"]


def test_long_content_must_be_split():
    with pytest.raises(ValidationError, match="Split it into separate items"):
        CommitIn(repo="r", message="m", items=[item("fact", "Everything", "word " * 80)])
    with pytest.raises(ValidationError, match="2-5 words"):
        CommitIn(repo="r", message="m", items=[item("fact", "x" * 70, "Short.")])


def test_same_question_uses_subjects():
    s = get_settings()
    a, b = np.array([1.0, 0.0]), np.array([0.95, 0.31])  # cosine ~0.95
    assert same_question(s, "auth provider", a, "authentication provider", b)
    assert not same_question(s, "detection model", a, "cost per vehicle", np.array([0.0, 1.0]))
    assert same_question(s, "region", None, "region", None)  # identical subjects
