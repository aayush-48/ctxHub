"""Request schemas shared by the REST API and MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import get_settings

ItemType = Literal["goal", "constraint", "decision", "open_question", "fact", "task"]
FlagChoice = Literal["keep_both", "supersede", "drop"]
ConflictChoice = Literal["ours", "theirs", "both", "edited"]

_S = get_settings()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ItemIn(_Strict):
    type: ItemType
    subject: str = Field(
        min_length=1,
        description="The question this item answers, in 2-5 words (e.g. 'Primary datastore').",
    )
    content: str = Field(
        min_length=1,
        description="One fact, in one or two self-contained sentences.",
    )
    reason: str | None = Field(default=None, max_length=500, description="Why, for decisions.")
    pinned: bool = Field(default=False, description="Never drop this item from renders.")
    replaces: str | None = Field(
        default=None,
        description="The stored item this replaces: its subject, or its id (or a unique prefix).",
    )
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("subject")
    @classmethod
    def _short_subject(cls, v: str) -> str:
        if len(v) > _S.max_subject_chars:
            raise ValueError(
                f"subject is {len(v)} characters; keep it to {_S.max_subject_chars}: name the "
                "question in 2-5 words (e.g. 'Detection model')"
            )
        return v

    @field_validator("content")
    @classmethod
    def _one_fact(cls, v: str) -> str:
        if len(v) > _S.max_content_chars:
            raise ValueError(
                f"content is {len(v)} characters (max {_S.max_content_chars}): an item holds one "
                "fact. Split it into separate items, each with its own specific subject"
            )
        return v

    @field_validator("attributes")
    @classmethod
    def _small_attrs(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(str(v)) > 2000:
            raise ValueError("attributes too large (max 2000 characters serialized)")
        return v


class StatusUpdateIn(_Strict):
    item: str = Field(description="The stored item: its subject, or its id (or a unique prefix).")
    status: Literal["resolved", "retracted"]
    note: str | None = Field(default=None, max_length=500)


class CommitIn(_Strict):
    repo: str
    message: str = Field(min_length=1, max_length=500)
    items: list[ItemIn] = Field(default_factory=list, max_length=_S.max_items_per_commit)
    updates: list[StatusUpdateIn] = Field(default_factory=list, max_length=_S.max_items_per_commit)
    branch: str | None = None
    source_client: str | None = Field(default=None, max_length=50)
    source_model: str | None = Field(default=None, max_length=100)
    auto_confirm: bool | None = Field(
        default=None,
        description="Commit immediately when the diff only adds items. Defaults to the repo "
        "setting.",
    )

    @model_validator(mode="after")
    def _non_empty(self) -> CommitIn:
        if not self.items and not self.updates:
            raise ValueError("a commit needs at least one item or status update")
        return self


class ConfirmIn(_Strict):
    accept: list[str] | None = Field(
        default=None, description="Entry ids to accept. Omit to accept every entry."
    )
    choices: dict[str, FlagChoice] = Field(
        default_factory=dict, description="For flagged entries: keep_both, supersede or drop."
    )


class EditedItemIn(_Strict):
    type: ItemType
    subject: str = Field(min_length=1, max_length=_S.max_subject_chars)
    content: str = Field(min_length=1, max_length=_S.max_content_chars)
    reason: str | None = Field(default=None, max_length=500)


class ResolveIn(_Strict):
    choice: ConflictChoice
    edited: EditedItemIn | None = None

    @model_validator(mode="after")
    def _edited_needed(self) -> ResolveIn:
        if self.choice == "edited" and self.edited is None:
            raise ValueError("choice 'edited' needs an edited item")
        return self


class RepoCreateIn(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    description: str | None = Field(default=None, max_length=1000)


class RepoSettingsIn(_Strict):
    auto_commit_additive: bool | None = None
    auto_merge_additive: bool | None = None


class BranchCreateIn(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
    from_ref: str | None = None


class MemberIn(_Strict):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["editor", "viewer", "owner"]


class PullIn(_Strict):
    ref: str | None = None
    model: str = Field(default="claude", max_length=100)
    # None: the recommended size (every item, shortened where possible, nothing dropped).
    budget: int | None = Field(default=None, ge=50, le=200_000)
    template: Literal["auto", "claude", "gpt", "plain"] = "auto"
    # [id:xxxxxxxx] after each item, for models that will edit the repo (ctx_stored gives
    # them too); left out of chat pulls to save tokens.
    ids: bool = True
    # "auto": a small repo loads whole, a large one "where you left off" unless a budget is
    # given; "all": everything that fits the budget; "starter": what always matters plus the
    # latest changes. A focus loads those plus the items that match it.
    mode: Literal["auto", "all", "starter"] = "auto"
    focus: str | None = Field(default=None, max_length=300)
    allow_drop_constraints: bool = False


def field_errors(e: ValidationError) -> list[dict[str, Any]]:
    """Field-level errors a model can act on to fix and retry its payload."""
    return [
        {"field": ".".join(str(p) for p in err["loc"]), "error": err["msg"]}
        for err in e.errors(include_url=False)
    ]
