"""Each user sees the repositories they own or were shared with, and nothing else."""

from __future__ import annotations

from ctx_server import service
from ctx_server.schemas import MemberIn, RepoCreateIn


async def test_lists_owned_and_shared_repos_only(alice, bob, repo):
    name = (await service.get_repo(alice, repo))["name"]  # the fixture yields the repo id
    other = f"{name}-bob"
    await service.create_repo(bob, RepoCreateIn(name=other))
    assert {r["name"]: r["role"] for r in await service.list_my_repos(bob)} == {other: "owner"}

    await service.set_member(alice, repo, MemberIn(email=bob.email, role="viewer"))
    mine = {r["name"]: r for r in await service.list_my_repos(bob)}
    assert mine[name]["role"] == "viewer" and mine[name]["owner"] == alice.email
    assert mine[other]["role"] == "owner"
    # Alice never sees Bob's repo.
    assert other not in {r["name"] for r in await service.list_my_repos(alice)}

    await service.set_member(alice, repo, MemberIn(email=bob.email, role="editor"))
    assert {r["name"]: r["role"] for r in await service.list_my_repos(bob)}[name] == "editor"

    await service.remove_member(alice, repo, bob.email)
    assert name not in {r["name"] for r in await service.list_my_repos(bob)}
