"""ctx: git-style version control for LLM context, from the terminal."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .client import ApiError, Client, NotLoggedIn
from .config import Config
from .login import browser_login, revoke

app = typer.Typer(
    help=__doc__, no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False
)
out = Console()
err = Console(stderr=True)


class State:
    json: bool = False
    repo: str | None = None
    server: str | None = None


S = State()

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
RepoOpt = Annotated[str | None, typer.Option("--repo", "-r", help="Repo (default: `ctx use`).")]


@app.callback()
def main(
    json_: JsonOpt = False,
    repo: RepoOpt = None,
    server: Annotated[str | None, typer.Option(help="Server URL (default: saved).")] = None,
) -> None:
    S.json, S.repo, S.server = json_, repo, server


def _cfg() -> Config:
    cfg = Config.load()
    if S.server:
        cfg.server = S.server
    return cfg


def _client() -> Client:
    return Client(_cfg())


def _repo(c: Client, repo: str | None = None) -> str:
    r = repo or S.repo or c.cfg.repo
    if not r:
        fail("no repo selected; pass --repo or run `ctx use <repo>`")
    return r


def emit(data: Any, human: Any = None) -> None:
    if S.json or human is None:
        out.print_json(json.dumps(data, default=str))
    elif callable(human):
        human(data)
    else:
        out.print(human)


def fail(msg: str, details: Any = None, code: int = 1) -> None:
    if S.json:
        print(json.dumps({"error": msg, "details": details}), file=sys.stderr)
    else:
        err.print(f"[red]error:[/red] {msg}")
        if details:
            err.print_json(json.dumps(details, default=str))
    raise typer.Exit(code)


def short(i: str | None) -> str:
    return (i or "").replace("-", "")[:8]


# ----------------------------------------------------------------------------- printing

ACTION_STYLE = {
    "add": "green",
    "supersede": "yellow",
    "refine": "cyan",
    "resolve": "blue",
    "drop_duplicate": "dim",
    "flag": "magenta",
    "status": "blue",
}


def print_entries(entries: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for col in ("id", "action", "type", "content", "reason"):
        t.add_column(col, overflow="fold")
    for e in entries:
        it = e.get("item") or {}
        target = e.get("target") or {}
        content = it.get("content") or target.get("content", "")
        if e["action"] in ("supersede", "refine", "flag") and target.get("content"):
            content += f"\n[dim]vs: {target['content']}[/dim]"
        if e["action"] in ("resolve", "status"):
            content = f"{target.get('content', '')}"
        reason = e.get("reason") or ""
        if e["action"] == "status":
            reason = f"mark {e.get('status')}" + (f": {reason}" if reason else "")
        style = ACTION_STYLE.get(e["action"], "")
        t.add_row(e["id"], f"[{style}]{e['action']}[/{style}]", it.get("type", ""), content, reason)
    out.print(t)


def print_items(items: list[dict]) -> None:
    by_type: dict[str, list[dict]] = {}
    for i in items:
        by_type.setdefault(i["type"], []).append(i)
    order = ["constraint", "goal", "decision", "open_question", "task", "fact"]
    for typ in order:
        if typ not in by_type:
            continue
        out.print(f"[bold]{typ.replace('_', ' ')}s[/bold]")
        for i in by_type[typ]:
            status = "" if i["status"] == "active" else f" [dim]({i['status']})[/dim]"
            star = "★ " if i.get("pinned") else ""
            out.print(f"  [dim]{short(i['item_key'])}[/dim] {star}{i['content']}{status}")


# ----------------------------------------------------------------------------- auth


@app.command()
def login(
    server: Annotated[
        str | None, typer.Argument(help="Server URL, e.g. https://ctx.example.com")
    ] = None,
    no_browser: Annotated[bool, typer.Option(help="Print the URL instead of opening it.")] = False,
) -> None:
    """Sign in through your browser."""
    cfg = _cfg()
    if server:
        cfg.server = server.rstrip("/")
    try:
        client_id, tok = browser_login(cfg.server, open_browser=not no_browser)
    except Exception as e:  # network, timeout, denied
        fail(f"login failed: {e}")
    cfg.client_id = client_id
    cfg.access_token = tok["access_token"]
    cfg.refresh_token = tok.get("refresh_token")
    cfg.save()
    me = Client(cfg).get("/me")
    emit(me, f"Logged in to {cfg.server} as [bold]{me['email']}[/bold]")


@app.command()
def logout() -> None:
    """Revoke the saved tokens and forget them."""
    cfg = _cfg()
    revoke(cfg.server, cfg.client_id, cfg.refresh_token)
    cfg.access_token = cfg.refresh_token = cfg.client_id = None
    cfg.save()
    emit({"logged_out": True}, "Logged out.")


@app.command()
def whoami() -> None:
    """Show the signed-in user and server."""
    c = _client()
    me = c.get("/me")
    emit(
        {**me, "server": c.cfg.server, "repo": c.cfg.repo},
        f"{me['email']} on {c.cfg.server}" + (f" (repo: {c.cfg.repo})" if c.cfg.repo else ""),
    )


# ----------------------------------------------------------------------------- repos


@app.command()
def init(
    name: str,
    description: Annotated[str | None, typer.Option("-d")] = None,
    use: Annotated[bool, typer.Option(help="Make it the default repo.")] = True,
) -> None:
    """Create a repository with a main branch."""
    c = _client()
    r = c.post("/repos", {"name": name, "description": description})
    if use:
        c.cfg.repo = name
        c.cfg.save()
    emit(r, f"Created [bold]{name}[/bold] on branch main")


@app.command()
def use(repo: str) -> None:
    """Set the default repo for later commands."""
    c = _client()
    c.get(f"/repos/{repo}")
    c.cfg.repo = repo
    c.cfg.save()
    emit({"repo": repo}, f"Using [bold]{repo}[/bold]")


@app.command()
def status() -> None:
    """Branch, head commit, item counts and pending merges."""
    c = _client()
    st = c.get(f"/repos/{_repo(c)}/status")

    def show(st: dict) -> None:
        where = (
            f"branch [bold]{st['branch']}[/bold]" if st["branch"] else "[yellow]detached[/yellow]"
        )
        out.print(f"{st['repo']} on {where} at {short(st['head'])} ({st['head_message']})")
        counts = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in st["active_items"].items())
        out.print(f"active: {st['active_total']} items" + (f" ({counts})" if counts else ""))
        for m in st["pending_merge"]:
            out.print(
                f"[magenta]pending merge from {m['from']}[/magenta]: "
                f"{m['open_conflicts']} open conflicts (merge {short(m['merge_id'])})"
            )
        if st["pending_proposals"]:
            out.print(f"{st['pending_proposals']} proposal(s) waiting for confirmation")

    emit(st, show)


@app.command("items")
def items_cmd(
    ref: Annotated[str | None, typer.Argument()] = None,
    all_: Annotated[bool, typer.Option("--all", help="Include closed items.")] = False,
) -> None:
    """Show the state items at a ref (default: your checkout)."""
    c = _client()
    r = c.get(f"/repos/{_repo(c)}/items", ref=ref, all=str(all_).lower())
    emit(r, lambda r: print_items(r["items"]))


@app.command()
def pin(
    item: Annotated[str, typer.Argument(help="Item id or key prefix (shown by `ctx items`).")],
    off: Annotated[bool, typer.Option("--off", help="Unpin instead.")] = False,
) -> None:
    """Pin an item so renders never drop it (no commit is created)."""
    c = _client()
    r = c.post(f"/repos/{_repo(c)}/items/{item}/pin", {"pinned": not off})
    emit(r, f"{'Unpinned' if off else 'Pinned'}: {r['content']}")


# ----------------------------------------------------------------------------- commit


def _confirm_flow(c: Client, result: dict, yes: bool, accept: str | None = None) -> None:
    if result.get("committed"):
        emit(result, f"Committed {short(result['commit'])}: {result['counts']}")
        return
    if S.json and not yes:
        emit(result)
        return
    print_entries(result["entries"])
    if not yes and not typer.confirm("Apply these changes?", default=True):
        c.delete(f"/proposals/{result['proposal_id']}")
        out.print("Discarded.")
        return
    body: dict[str, Any] = {}
    if accept:
        body["accept"] = [a.strip() for a in accept.split(",") if a.strip()]
    done = c.post(f"/proposals/{result['proposal_id']}/confirm", body)
    emit(done, f"Committed {short(done['commit'])} on {done['branch']}: {done['counts']}")


@app.command()
def commit(
    message: Annotated[str, typer.Option("-m", "--message")],
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            "-f",
            exists=True,
            dir_okay=False,
            help="JSON: a list of items, or {items, updates}.",
        ),
    ],
    branch: Annotated[str | None, typer.Option("-b")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm without asking.")] = False,
    no_auto: Annotated[bool, typer.Option(help="Always show the diff first.")] = False,
) -> None:
    """Commit typed items from a JSON file."""
    c = _client()
    data = json.loads(file.read_text(encoding="utf-8"))
    payload = {"items": data} if isinstance(data, list) else data
    body = {
        "repo": _repo(c),
        "message": message,
        "branch": branch,
        "source_client": "cli",
        **payload,
    }
    if no_auto:
        body["auto_confirm"] = False
    _confirm_flow(c, c.post("/commits", body), yes)


@app.command()
def confirm(
    proposal_id: str,
    accept: Annotated[str | None, typer.Option(help="Comma-separated entry ids.")] = None,
    choice: Annotated[list[str] | None, typer.Option(help="entry=keep_both|supersede|drop")] = None,
) -> None:
    """Confirm a pending proposal (for example one a chat model created)."""
    c = _client()
    body: dict[str, Any] = {}
    if accept:
        body["accept"] = [a.strip() for a in accept.split(",")]
    if choice:
        body["choices"] = dict(x.split("=", 1) for x in choice)
    done = c.post(f"/proposals/{proposal_id}/confirm", body)
    emit(done, f"Committed {short(done['commit'])} on {done['branch']}: {done['counts']}")


@app.command()
def log(
    ref: Annotated[str | None, typer.Argument()] = None,
    n: Annotated[int, typer.Option("-n")] = 20,
    all_: Annotated[bool, typer.Option("--all", help="All branches.")] = False,
) -> None:
    """Commit history."""
    c = _client()
    r = c.get(f"/repos/{_repo(c)}/log", ref=ref, limit=n, all=str(all_).lower())

    def show(r: dict) -> None:
        heads: dict[str, list[str]] = {}
        for b in r["branches"]:
            heads.setdefault(b["head_commit_id"], []).append(b["name"])
        for cm in r["commits"]:
            mark = "M " if len(cm["parents"]) > 1 else "* "
            names = heads.get(cm["id"])
            label = f" [cyan]({', '.join(names)})[/cyan]" if names else ""
            src = "/".join(x for x in (cm["source_client"], cm["source_model"]) if x)
            out.print(
                f"{mark}[yellow]{short(cm['id'])}[/yellow]{label} {cm['message']} "
                f"[dim]{cm['author']} {src} · {cm['active_items']} items · "
                f"{cm['created_at'][:16]}[/dim]"
            )

    emit(r, show)


def _print_diff(d: dict) -> None:
    labels = [
        ("added", "+", "green"),
        ("superseded", "~", "yellow"),
        ("resolved", "✓", "blue"),
        ("retracted", "-", "red"),
        ("reactivated", "↺", "cyan"),
        ("removed", "-", "red"),
    ]
    any_ = False
    for key, sym, col in labels:
        for i in d.get(key, []):
            any_ = True
            out.print(f"[{col}]{sym} {key:<11}[/{col}] [dim]{i['type']}[/dim] {i['content']}")
    if not any_:
        out.print("no changes")


@app.command()
def diff(
    a: Annotated[str, typer.Argument()] = "HEAD~1",
    b: Annotated[str | None, typer.Argument()] = None,
) -> None:
    """Items added, superseded, resolved or retracted between two refs."""
    c = _client()
    emit(c.get(f"/repos/{_repo(c)}/diff", **{"from": a, "to": b}), _print_diff)


@app.command()
def show(
    ref: Annotated[str, typer.Argument()] = "HEAD",
    audit: Annotated[bool, typer.Option(help="Show pipeline decisions.")] = False,
) -> None:
    """Show a commit, its changes and (optionally) the pipeline audit log."""
    c = _client()
    r = c.get(f"/repos/{_repo(c)}/commits/{ref}")

    def show_(r: dict) -> None:
        cm = r["commit"]
        out.print(f"[yellow]commit {cm['id']}[/yellow]  ({cm['kind']})")
        out.print(
            f"Author: {cm['author']}  Source: {cm['source_client']} {cm['source_model'] or ''}"
        )
        out.print(f"Date:   {cm['created_at']}\n\n    {cm['message']}\n")
        _print_diff(r["diff"])
        if audit:
            out.print("\n[bold]pipeline audit[/bold]")
            for a in r["audit"]:
                out.print(
                    f"  {a['stage']:<10} {a['decision']:<22} "
                    f"[dim]{json.dumps(a['details'], default=str)[:160]}[/dim]"
                )

    emit(r, show_)


# ----------------------------------------------------------------------------- branches


@app.command()
def branch(
    name: Annotated[str | None, typer.Argument()] = None,
    from_: Annotated[str | None, typer.Option("--from")] = None,
    delete: Annotated[bool, typer.Option("-d", "--delete")] = False,
) -> None:
    """List branches, create one, or delete one."""
    c = _client()
    repo = _repo(c)
    if name is None:
        rows = c.get(f"/repos/{repo}/branches")
        current = c.get(f"/repos/{repo}/status")["branch"]
        emit(
            rows,
            lambda rows: [
                out.print(
                    ("* " if b["name"] == current else "  ")
                    + b["name"]
                    + f" [dim]{short(b['head_commit_id'])}[/dim]"
                )
                for b in rows
            ],
        )
    elif delete:
        c.delete(f"/repos/{repo}/branches/{name}")
        emit({"deleted": name}, f"Deleted branch {name}")
    else:
        r = c.post(f"/repos/{repo}/branches", {"name": name, "from_ref": from_})
        emit(r, f"Created branch [bold]{name}[/bold] at {short(r['head'])}")


@app.command()
def checkout(ref: str) -> None:
    """Switch to a branch or commit."""
    c = _client()
    r = c.post(f"/repos/{_repo(c)}/checkout", {"ref": ref})
    emit(
        r,
        f"On branch [bold]{r['branch']}[/bold]"
        if r["branch"]
        else f"[yellow]Detached at {short(r['head'])}[/yellow]",
    )


@app.command()
def revert(commit_ref: Annotated[str, typer.Argument(metavar="COMMIT")]) -> None:
    """Undo a commit with a new commit."""
    c = _client()
    r = c.post(f"/repos/{_repo(c)}/revert", {"commit": commit_ref})
    emit(r, f"Reverted {short(r['reverted'])} with {short(r['commit'])} on {r['branch']}")


# ----------------------------------------------------------------------------- merge


def _conflict_text(cf: dict) -> None:
    out.print(
        f"\n[magenta]conflict {short(cf['conflict_id'])}[/magenta] ({cf['kind']})"
        + (f": {cf['reason']}" if cf.get("reason") else "")
    )
    if cf.get("base"):
        out.print(f"  base:   {cf['base']['content']}")
    out.print(f"  ours:   {(cf.get('ours') or {}).get('content', '(removed)')}")
    out.print(f"  theirs: {(cf.get('theirs') or {}).get('content', '(removed)')}")


def _resolve_interactive(c: Client, merge_id: str, conflicts: list[dict]) -> dict | None:
    result = None
    for cf in conflicts:
        if cf.get("resolution"):
            continue
        _conflict_text(cf)
        choice = typer.prompt("Keep [o]urs, [t]heirs, [b]oth, or [e]dit?", default="o")
        choice = {"o": "ours", "t": "theirs", "b": "both", "e": "edited"}.get(choice[:1], "ours")
        body: dict[str, Any] = {"choice": choice}
        if choice == "edited":
            src = cf.get("ours") or cf.get("theirs") or {}
            body["edited"] = {
                "type": src.get("type", "decision"),
                "subject": typer.prompt("subject", default=src.get("subject", "")),
                "content": typer.prompt("content", default=src.get("content", "")),
            }
        result = c.post(f"/merges/{merge_id}/conflicts/{cf['conflict_id']}/resolve", body)
    return result


@app.command()
def merge(
    source: Annotated[str | None, typer.Argument(help="Branch or commit to merge in.")] = None,
    abort: Annotated[bool, typer.Option("--abort")] = False,
    continue_: Annotated[
        bool, typer.Option("--continue", help="Resolve pending conflicts.")
    ] = False,
    review: Annotated[bool, typer.Option(help="Do not auto-merge; review first.")] = False,
) -> None:
    """Merge a branch by meaning; conflicts are resolved interactively."""
    c = _client()
    repo = _repo(c)
    if abort or continue_:
        pending = c.get(f"/repos/{repo}/status")["pending_merge"]
        if not pending:
            fail("no pending merge on this branch")
        mid = pending[0]["merge_id"]
        if abort:
            emit(c.post(f"/merges/{mid}/abort"), "Merge aborted.")
            return
        m = c.get(f"/merges/{mid}")
    else:
        if not source:
            fail("name a branch to merge")
        m = c.post(f"/repos/{repo}/merges", {"source": source, "review": review})
        if m["result"] in ("up_to_date", "fast_forward", "merged"):
            emit(
                m,
                {
                    "up_to_date": "Already up to date.",
                    "fast_forward": f"Fast-forwarded {m['branch']} to {short(m.get('head'))}",
                    "merged": f"Merged into {m['branch']}: {short(m.get('commit'))} "
                    f"{m.get('summary')}",
                }[m["result"]],
            )
            return
        mid = m["merge_id"]
    if S.json:
        emit(m)
        return
    out.print(f"{len(m['conflicts'])} conflict(s). Summary: {m.get('summary')}")
    for rv in m.get("review") or []:
        out.print(
            f"[cyan]review[/cyan]: kept '{rv['kept']['content']}' over "
            f"'{rv['superseded']['content']}'"
        )
    if not m["conflicts"]:
        if typer.confirm("Complete the merge?", default=True):
            done = c.post(f"/merges/{mid}/complete")
            out.print(f"Merged: {short(done['commit'])}")
        return
    result = _resolve_interactive(c, mid, m["conflicts"])
    if result and result.get("state") == "completed":
        out.print(f"Merged: {short(result['commit'])} on {result['branch']}")
    else:
        out.print("Conflicts remain; run `ctx merge --continue`.")


@app.command()
def resolve(
    conflict_id: str,
    choice: Annotated[str, typer.Argument(help="ours, theirs, both or edited")],
    content: Annotated[str | None, typer.Option(help="For 'edited'.")] = None,
    subject: Annotated[str | None, typer.Option(help="For 'edited'.")] = None,
    type_: Annotated[str, typer.Option("--type", help="For 'edited'.")] = "decision",
) -> None:
    """Resolve one conflict of the pending merge non-interactively."""
    c = _client()
    pending = c.get(f"/repos/{_repo(c)}/status")["pending_merge"]
    if not pending:
        fail("no pending merge on this branch")
    mid = pending[0]["merge_id"]
    m = c.get(f"/merges/{mid}")
    match = [
        cf
        for cf in m["conflicts"]
        if cf["conflict_id"].replace("-", "").startswith(conflict_id.replace("-", ""))
    ]
    if len(match) != 1:
        fail("conflict id not found or ambiguous")
    body: dict[str, Any] = {"choice": choice}
    if choice == "edited":
        if not content or not subject:
            fail("--subject and --content are required for 'edited'")
        body["edited"] = {"type": type_, "subject": subject, "content": content}
    emit(c.post(f"/merges/{mid}/conflicts/{match[0]['conflict_id']}/resolve", body))


# ----------------------------------------------------------------------------- pull


def _copy(text: str) -> bool:
    for cmd in (["clip"], ["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            subprocess.run(
                cmd, input=text.encode("utf-16" if cmd[0] == "clip" else "utf-8"), check=False
            )
            return True
    return False


@app.command()
def pull(
    model: Annotated[str, typer.Option(help="Target model: claude, gpt-5, ...")] = "claude",
    budget: Annotated[int, typer.Option(help="Token budget.")] = 4000,
    ref: Annotated[str | None, typer.Option()] = None,
    copy: Annotated[bool, typer.Option(help="Copy to the clipboard.")] = False,
    output: Annotated[Path | None, typer.Option("-o")] = None,
    allow_drop_constraints: bool = False,
) -> None:
    """Render the state for a model within a token budget."""
    c = _client()
    r = c.post(
        f"/repos/{_repo(c)}/pull",
        {
            "ref": ref,
            "model": model,
            "budget": budget,
            "allow_drop_constraints": allow_drop_constraints,
        },
    )
    if output:
        output.write_text(r["context"], encoding="utf-8")
    if copy and not _copy(r["context"]):
        err.print("[yellow]no clipboard tool found[/yellow]")
    if S.json:
        emit(r)
        return
    if not output and not copy:
        print(r["context"])
    m = r["manifest"]
    exact = "" if m["token_count_exact"] else " (estimated)"
    err.print(
        f"[dim]{m['tokens']}/{m['budget']} tokens{exact} · {m['included']} items · "
        f"{len(m['compressed'])} compressed · {len(m['dropped'])} dropped · "
        f"template {m['template']}[/dim]"
    )


# ----------------------------------------------------------------------------- sharing


@app.command()
def members() -> None:
    """List who has access to the repo."""
    c = _client()
    emit(
        c.get(f"/repos/{_repo(c)}/members"),
        lambda rows: [out.print(f"{r['role']:<7} {r['email']}") for r in rows],
    )


@app.command()
def share(email: str, role: Annotated[str, typer.Option()] = "viewer") -> None:
    """Give someone access (owner, editor or viewer). They must have signed in once."""
    c = _client()
    emit(
        c.request("PUT", f"/repos/{_repo(c)}/members", json={"email": email, "role": role}),
        f"{email} is now {role}",
    )


@app.command()
def unshare(email: str) -> None:
    """Remove someone's access."""
    c = _client()
    c.delete(f"/repos/{_repo(c)}/members/{email}")
    emit({"removed": email}, f"Removed {email}")


@app.command()
def settings(
    auto_commit: Annotated[bool | None, typer.Option(help="Auto-commit additive diffs.")] = None,
    auto_merge: Annotated[bool | None, typer.Option(help="Auto-merge without conflicts.")] = None,
) -> None:
    """View or change repo settings."""
    c = _client()
    repo = _repo(c)
    body = {
        k: v
        for k, v in {
            "auto_commit_additive": auto_commit,
            "auto_merge_additive": auto_merge,
        }.items()
        if v is not None
    }
    r = (
        c.request("PATCH", f"/repos/{repo}/settings", json=body)
        if body
        else c.get(f"/repos/{repo}")["settings"]
    )
    emit(r)


@app.command("delete-repo")
def delete_repo(repo: str, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Permanently delete a repo, its history and its uploads."""
    if not yes and not typer.confirm(f"Permanently delete {repo}? This cannot be undone."):
        raise typer.Exit(1)
    c = _client()
    c.delete(f"/repos/{repo}")
    emit({"deleted": repo}, f"Deleted {repo}")


def run() -> None:
    try:
        app()
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except (NotLoggedIn, ApiError) as e:
        details = getattr(e, "details", None)
        if S.json:
            print(json.dumps({"error": str(e), "details": details}), file=sys.stderr)
        else:
            err.print(f"[red]error:[/red] {e}")
            if details:
                err.print_json(json.dumps(details, default=str))
        sys.exit(2 if isinstance(e, NotLoggedIn) else 1)


if __name__ == "__main__":
    run()
