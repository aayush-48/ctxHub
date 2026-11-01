"""The CLI against a real server: init, commit, log, branch, merge, pull, import, --json."""

from __future__ import annotations

import io
import json
import zipfile

from ctx_cli.main import S, app
from typer.testing import CliRunner

runner = CliRunner()


def ctx(*args: str, input: str | None = None):
    S.json, S.repo, S.server = False, None, None  # reset global options between invocations
    r = runner.invoke(app, list(args), input=input, catch_exceptions=False)
    assert r.exit_code == 0, r.output
    return r.output


def ctxj(*args: str):
    return json.loads(ctx("--json", *args))


def test_cli_end_to_end(cli_env, tmp_path):
    ctx("init", "demo")
    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            [
                {"type": "constraint", "subject": "Hosting", "content": "Must run on GCP."},
                {
                    "type": "decision",
                    "subject": "Queue",
                    "content": "Use Cloud Tasks.",
                    "reason": "Managed, cheap",
                },
            ]
        )
    )
    r = ctxj("commit", "-m", "First state", "-f", str(items))
    assert r["committed"] and r["counts"]["added"] == 2

    st = ctxj("status")
    assert st["active_total"] == 2 and st["branch"] == "main"

    ctx("branch", "alt")
    ctx("checkout", "alt")
    items.write_text(
        json.dumps([{"type": "decision", "subject": "Queue", "content": "Use Pub/Sub."}])
    )
    out = ctx("commit", "-m", "Try Pub/Sub", "-f", str(items), "--yes")
    assert "Committed" in out
    ctx("checkout", "main")

    m = ctxj("merge", "alt")
    assert m["result"] == "fast_forward"
    assert [i["content"] for i in ctxj("items")["items"] if i["type"] == "decision"] == [
        "Use Pub/Sub."
    ]

    log = ctxj("log")
    assert [c["message"] for c in log["commits"]][:2] == ["Try Pub/Sub", "First state"]
    d = ctxj("diff", "HEAD~1")
    assert len(d["added"]) == 1 and len(d["superseded"]) == 1

    pulled = ctxj("pull", "--model", "gpt-5", "--budget", "500")
    assert pulled["context"].startswith("# Project context")
    assert pulled["manifest"]["tokens"] <= 500

    human = ctx("show", "--audit")
    assert "pipeline audit" in human and "supersede" in human

    reverted = ctxj("revert", "HEAD")
    assert reverted["branch"] == "main"


def test_login_through_browser_flow(server_url, tmp_path, monkeypatch):
    """Runs the real `ctx login`: a fake browser walks sign-in and consent and lands on the
    CLI's loopback callback."""
    import re
    from urllib.parse import parse_qs, urlparse

    import ctx_cli.login as login_mod
    import httpx

    monkeypatch.setenv("CTX_CONFIG", str(tmp_path / "config.json"))

    def fake_browser(url: str) -> bool:
        with httpx.Client(base_url=server_url) as b:
            r = b.get(url)
            req = parse_qs(urlparse(r.headers["location"]).query)["req"][0]
            b.post("/auth/dev", data={"req": req, "email": "login-test@example.com"})
            page = b.get(f"/consent?req={req}")
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
            r = b.post("/consent", data={"req": req, "csrf": csrf, "decision": "approve"})
            assert "Signed in to ctx" in httpx.get(r.headers["location"]).text
        return True

    monkeypatch.setattr(login_mod.webbrowser, "open", fake_browser)
    out = ctx("login", server_url)
    assert "login-test@example.com" in out
    assert ctxj("whoami")["email"] == "login-test@example.com"


def test_cli_merge_conflict_interactive(cli_env, tmp_path):
    ctx("init", "conflicts")
    f = tmp_path / "i.json"
    f.write_text(json.dumps([{"type": "goal", "subject": "Goal", "content": "Ship it."}]))
    ctx("commit", "-m", "base", "-f", str(f))
    ctx("branch", "b")
    f.write_text(json.dumps([{"type": "decision", "subject": "DB", "content": "Use MySQL."}]))
    ctx("commit", "-m", "ours", "-f", str(f))
    ctx("checkout", "b")
    f.write_text(json.dumps([{"type": "decision", "subject": "DB", "content": "Use Postgres."}]))
    ctx("commit", "-m", "theirs", "-f", str(f))
    ctx("checkout", "main")
    out = ctx("merge", "b", input="t\n")
    assert "conflict" in out and "Merged" in out
    decisions = [i["content"] for i in ctxj("items")["items"] if i["type"] == "decision"]
    assert decisions == ["Use Postgres."]


def test_cli_import_and_errors(cli_env, tmp_path):
    ctx("init", "imports")
    export = tmp_path / "export.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "conversations.json",
            json.dumps(
                [
                    {
                        "uuid": "u1",
                        "name": "Plan",
                        "chat_messages": [
                            {
                                "sender": "human",
                                "content": [
                                    {"type": "text", "text": "task: Schema | Write the schema."}
                                ],
                            }
                        ],
                    }
                ]
            ),
        )
    export.write_bytes(buf.getvalue())
    out = ctx("import", str(export), "--yes")
    assert "Committed" in out
    assert ctxj("status")["active_items"] == {"task": 1}

    S.json = False
    r = runner.invoke(app, ["--repo", "nope", "status"])
    assert r.exit_code != 0
