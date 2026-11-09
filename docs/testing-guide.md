# Testing guide

A feature-by-feature checklist for testing ctx by hand against a deployed service, followed
by the benchmarks. Each test lists the steps and what you should see.

**Setup used throughout**

- `$URL` = your service URL (for example `https://ctx-910037083701.asia-south1.run.app`)
- Run commands in PowerShell from the repository folder (they use the files in `examples/`).
- Sign-in works for accounts added as test users while the Google OAuth app is in Testing mode.
- Results marked *(rule judgment)* come from the server's rules, embeddings and local NLI model.
  When they are unsure they flag the entry for you instead of guessing.

```powershell
uv tool install --reinstall ./cli
ctx login $URL
ctx init testing
```

---

## 1. Accounts and sign-in

- [ ] **1.1 Web sign-in.** Open `$URL` in a browser and choose **Continue with Google**.
  *Expect:* the repository list. No consent page, because the viewer is first-party.
- [ ] **1.2 CLI sign-in.** `ctx login $URL`
  *Expect:* the browser opens, then a consent page "Allow ctx CLI?". After **Allow**, the
  browser shows "Signed in to ctx." and the terminal prints your email.
- [ ] **1.3 Consent can be refused.** Run `ctx login $URL` again and click **Deny**.
  *Expect:* `login failed: sign-in failed: access_denied`. Then log in again normally.
- [ ] **1.4 Who am I.** `ctx whoami`
  *Expect:* your email, the server URL and the default repo.
- [ ] **1.5 Requests without a login are rejected.**
  ```powershell
  Invoke-WebRequest -Method Post "$URL/mcp" -Body '{}' -ContentType application/json
  Invoke-WebRequest "$URL/api/v1/repos"
  ```
  *Expect:* both fail with **401 Unauthorized**. The MCP response includes a
  `WWW-Authenticate` header that points clients at the sign-in metadata.
- [ ] **1.6 Logout revokes tokens.** `ctx logout`, then `ctx status`.
  *Expect:* `not logged in; run ctx login`. Log in again afterwards.
- [ ] **1.7 Sign-in metadata is published.** Open `$URL/.well-known/oauth-authorization-server`
  and `$URL/.well-known/oauth-protected-resource/mcp`.
  *Expect:* JSON listing the authorize, token, register and revoke endpoints and `S256` PKCE.

## 2. Repositories

- [ ] **2.1 Create.** `ctx init testing` (already done in setup)
  *Expect:* `Created testing on branch main`; it becomes the default repo.
- [ ] **2.2 Names are unique across everyone.** `ctx init testing` again, or `ctx init TESTING`.
  *Expect:* `the repo name 'testing' is already taken`. A second account gets the same
  answer.
- [ ] **2.4 Switch the default.** `ctx use testing`
- [ ] **2.5 Settings.** `ctx settings`
  *Expect:* `auto_commit_additive` and `auto_merge_additive` both true.

## 3. Commit pipeline

- [ ] **3.1 Field-level validation.** `ctx commit -m "bad" -f examples/invalid.json`
  *Expect:* `validation_failed`, naming `items.0.type` and `items.0.subject`. Nothing is written.
- [ ] **3.2 Additive commits auto-confirm.** `ctx commit -m "Initial plan" -f examples/state.json`
  *Expect:* `Committed <id>` with `added: 6`. No prompt, because the diff only adds items.
- [ ] **3.3 Diff and confirm; contradiction supersedes.**
  `ctx commit -m "Switch datastore" -f examples/change-datastore.json`
  *Expect:* a diff table, then a prompt.
  - The datastore decision is **supersede** ("vs: Use a separate vector DB").
  - The region fact is **drop_duplicate** (identical content).

  Answer `n` the first time and check that `ctx status` still shows 6 items. Then run the
  commit again and answer `y`.
- [ ] **3.4 Replaced items are linked, not deleted.** `ctx items --all`
  *Expect:* "Use a separate vector DB." marked `(superseded)`.
- [ ] **3.5 Paraphrase is recognized as a duplicate** *(rule judgment)*.
  `ctx commit -m "Restate hosting" -f examples/paraphrase.json`
  *Expect:* **drop_duplicate** against "Must run on GCP; no paid vector DB."
- [ ] **3.6 Refinement keeps the same item identity** *(rule judgment)*.
  `ctx commit -m "Refine MVP" -f examples/refine-goal.json`
  *Expect:* **refine**. After confirming, `ctx --json items --all` shows the old and new goal
  with the **same `item_key`**.
- [ ] **3.7 Answering an open question closes it** *(rule judgment)*.
  `ctx commit -m "ChatGPT answer" -f examples/answer-question.json`
  *Expect:* an **add** for the decision plus a **resolve** entry for the open question.
  After confirming, `ctx items` no longer lists the question.
- [ ] **3.8 Temporary situations are flagged, not applied** *(rule judgment)*.
  `ctx commit -m "Temporary region" -f examples/temporal.json --no-auto`
  *Expect:* **flag** with relation `temporal_scope` (or a low-confidence flag). Confirming
  keeps both facts by default.
- [ ] **3.9 Accept only some entries.** Create a pending proposal without a prompt (answering
  `n` at a prompt discards the proposal instead):
  `ctx --json commit -m "Partial" -f examples/many-facts.json --no-auto`
  Copy `proposal_id` from the output, then run `ctx confirm <proposal_id> --accept e1,e2`.
  *Expect:* only entries `e1` and `e2` are applied (`added: 2`).
- [ ] **3.10 Choice for a flagged entry.** Make a pending proposal from the flagged commit in
  3.8 (`ctx --json commit -m "Temp 2" -f examples/temporal.json --no-auto`), then run
  `ctx confirm <proposal_id> --choice e1=supersede`.
  *Expect:* the flagged item replaces the old one instead of keeping both.
- [ ] **3.11 Mark items resolved or retracted.** Get a task's `item_key` from
  `ctx --json items`, then create `done.json`:
  ```json
  {"items": [], "updates": [{"item": "<first 8 chars of item_key>", "status": "resolved"}]}
  ```
  `ctx commit -m "Task done" -f done.json --yes`
  *Expect:* `resolved: 1`; the task disappears from `ctx items`.
- [ ] **3.12 Stale proposals are refused.** Create a pending proposal with
  `ctx --json commit -m "Stale" -f examples/paraphrase.json --no-auto` and copy its
  `proposal_id`. Commit something else (`ctx commit -m "Other" -f examples/refine-goal.json --yes`),
  then `ctx confirm <proposal_id>`.
  *Expect:* `branch 'main' moved since this proposal was made; run the commit again`.
- [ ] **3.13 Audit log.** `ctx show --audit`
  *Expect:* lines for normalize, embed, dedupe/classify, supersede, score, compress and
  confirm, with the rule that decided each one (and NLI scores where used).
- [ ] **3.14 An explicit replacement always wins.** Get an item's id from `ctx pull` (the
  `[id:xxxxxxxx]` at the end of each line), then commit an item with `"replaces": "<that id>"`.
  *Expect:* **refine** (same type) or **supersede**, with relation `explicit`, whatever the
  rules would have said. This is how your chat model marks changes of mind.

## 4. History

- [ ] **4.1 Log.** `ctx log` and `ctx log --all`
  *Expect:* commits with author, client/model, item counts; branch names on their heads.
- [ ] **4.2 Diff between refs.** `ctx diff HEAD~2` and `ctx diff HEAD~2 HEAD~1`
  *Expect:* added, superseded and resolved items between the two points.
- [ ] **4.3 Show a commit.** `ctx show HEAD~1`
- [ ] **4.4 Commit-id prefixes work.** Copy 6–8 characters of a commit id from `ctx log`, then
  run `ctx show <prefix>`.
- [ ] **4.5 Detached checkout refuses writes.** `ctx checkout HEAD~1`, then
  `ctx commit -m x -f examples/paraphrase.json`.
  *Expect:* `you are on a detached commit; create a branch first`. Then run `ctx checkout main`.
- [ ] **4.6 Revert.** `ctx revert HEAD`, then `ctx log -n 3`.
  *Expect:* a new `Revert "..."` commit; the previous state is back; nothing deleted.
- [ ] **4.7 Revert conflicts are detected.** Revert an older commit whose items were changed
  again later (for example the "Switch datastore" commit after another datastore change).
  *Expect:* `revert conflicts with later changes`, listing the items.

## 5. Branch and merge

- [ ] **5.1 Branch list, create, delete.** `ctx branch`, `ctx branch tmp`, `ctx branch tmp -d`,
  and `ctx branch main -d`.
  *Expect:* the last one fails with `cannot delete main`.
- [ ] **5.2 Fast-forward.**
  ```powershell
  ctx branch ff; ctx checkout ff
  ctx commit -m "On ff" -f examples/many-facts.json
  ctx checkout main; ctx merge ff
  ```
  *Expect:* `Fast-forwarded main ...`. Merging again prints `Already up to date.`
- [ ] **5.3 Contradiction conflict across branches.**
  ```powershell
  ctx branch alt --from HEAD
  ctx checkout alt
  ctx commit -m "Firestore" -f examples/alternative.json --yes
  ctx checkout main
  '[{"type":"decision","subject":"Primary datastore","content":"Use AlloyDB."}]' | Out-File -Encoding utf8 alloy.json
  ctx commit -m "AlloyDB" -f alloy.json --yes
  ctx merge alt
  ```
  *Expect:* exactly **1 conflict** (ours AlloyDB vs theirs Firestore). Never "latest wins".
- [ ] **5.4 Resolve: theirs / ours / both / edit.** At the prompt try each across runs:
  `t`, `o`, `b`, or `e` (type a new subject and content).
  *Expect:* `Merged: <id>`, and `ctx items` shows the chosen result. `ctx log --all` shows a
  merge commit with two parents.
- [ ] **5.5 Clean merge when both branches make the same change.** Create two branches from
  the same commit and commit the identical `examples/alternative.json` on both, then merge.
  *Expect:* `Merged` with no conflicts.
- [ ] **5.6 One-sided changes merge automatically.** Change a fact on a branch and a
  different decision on main, then merge.
  *Expect:* both changes present, no conflicts.
- [ ] **5.7 Pending merge warnings in renders.** Create a conflict (as in 5.3), run
  `ctx --json merge alt` so it stays pending, then `ctx pull --budget 1500`.
  *Expect:* an "Unresolved conflicts" section describing both sides.
- [ ] **5.8 One pending merge per branch.** Run `ctx merge alt` again.
  *Expect:* `a merge into 'main' is already pending`.
- [ ] **5.9 Continue or abort.** `ctx merge --continue` (resolve interactively) or
  `ctx merge --abort`.
- [ ] **5.10 Review mode.** `ctx merge <branch> --review`, or turn auto-merge off with
  `ctx settings --no-auto-merge`.
  *Expect:* even a conflict-free merge waits; answer `y` to "Complete the merge?".
- [ ] **5.11 Non-interactive resolve.** With a pending conflict:
  `ctx resolve <first chars of conflict id> theirs`

## 6. Rendering (pull)

- [ ] **6.1 Templates per model.**
  `ctx pull --model claude --budget 1500` and `ctx pull --model gpt-5 --budget 1500`
  *Expect:* XML-style `<context>` sections for Claude; Markdown headings for GPT.
- [ ] **6.2 Budget is respected.** `ctx --json pull --budget 300`
  *Expect:* `manifest.tokens` ≤ 300; `dropped` and `compressed` list what was left out.
- [ ] **6.3 Sizes.** `ctx pull` with no budget.
  *Expect:* a second status line `sizes: minimum A · recommended B · full C`, and the pull uses
  B with **0 dropped**. `ctx pull --budget C` shows 0 shortened; a budget between B and C shows
  some shortened and still 0 dropped (every item is shortened before any is dropped); a budget
  below B drops the lowest-priority items.
- [ ] **6.4 Constraints are never dropped silently.** At any budget, `ctx pull --budget 200`
  still contains every constraint, even when other items are dropped. Only if the constraints
  alone exceed the budget do you get an error asking for a bigger one (it includes
  `minimum_budget`);
  `--allow-drop-constraints` overrides that.
- [ ] **6.5 Item ids in renders.** Each line ends with `[id:xxxxxxxx]`, which a model can use in
  `replaces` or `updates`.
- [ ] **6.6 Stored text can't break out of the context.**
  `ctx commit -m "Injection" -f examples/injection.json`, then `ctx pull --model claude`.
  *Expect:* the text appears escaped as `&lt;/context&gt;`; there is still exactly one
  closing `</context>`.
- [ ] **6.7 Copy and save.** `ctx pull --copy` (paste somewhere) and `ctx pull -o context.txt`.
- [ ] **6.8 Exact vs estimated counts.** `ctx --json pull --model gpt-5` shows
  `token_count_exact: true`; `--model claude` shows `false` (estimate with a safety margin).

## 7. Old chats and documents

The server does not read conversations; your chat model does.

- [ ] **7.1 Commit from an export or document.** Export a chat (Claude: Settings → Privacy →
  Export data; ChatGPT: Settings → Data controls → Export), attach the conversation file or
  any planning document in a Claude chat, and say "commit the task state of this file to
  testing".
  *Expect:* the model calls `ctx_pull` to see what is stored, then `ctx_commit` with typed
  items, and shows you the diff before confirming.
- [ ] **7.2 It marks changes itself.** In that chat, say "we changed our mind about the
  datastore, it's DynamoDB now; commit that".
  *Expect:* the commit uses `replaces` with the stored item's id (visible in the tool call),
  so the diff shows a supersede with relation `explicit`.

## 8. Sharing and roles (needs a second Google account added as a test user)

- [ ] **8.1 The other person signs in once** at `$URL`.
- [ ] **8.2 Unknown emails are refused.** `ctx share someone-new@example.com`
  *Expect:* `has not signed in yet`.
- [ ] **8.3 Viewer.** `ctx share <second email> --role viewer`
  *Expect, as the second account:* the repo appears in the web viewer; a commit fails with
  `forbidden: this needs the editor role`.
- [ ] **8.4 Editor.** `ctx share <second email> --role editor`
  *Expect:* the second account can commit, branch and merge, but not share.
- [ ] **8.5 Owner-only actions.** As the editor: `ctx share x@y.com`, `ctx settings --no-auto-commit`.
  *Expect:* forbidden.
- [ ] **8.6 Removal hides the repo.** `ctx unshare <second email>`
  *Expect, as the second account:* `repo not found` (not "forbidden", so names don't leak).
- [ ] **8.7 Two people committing at once.** Both create a pending proposal with
  `ctx --json commit ... --no-auto`, then both run `ctx confirm <their proposal_id>`.
  *Expect:* the second confirm is refused ("branch moved"); rerunning the commit works.

## 9. Claude and ChatGPT (MCP)

- [ ] **9.1 Connect.** Claude: Settings → Connectors → Add custom connector → `$URL/mcp`.
- [ ] **9.2 Tools list.** Ask "What ctx tools do you have?". *Expect:* six tools: `ctx_stored`, `ctx_commit`, `ctx_confirm`, `ctx_pull`, `ctx_inspect`, `ctx_manage` (no repo listing: you open repos by name).
- [ ] **9.3 Commit from chat.** Discuss a plan, then "Commit our decisions to testing."
  *Expect:* the model calls `ctx_stored` (which returns the commit rules), then `ctx_commit`,
  and either reports the commit or shows the diff and asks you before calling `ctx_confirm`.
  The commit result Claude reads is one line per entry; the review view still shows full items.
- [ ] **9.4 The model never confirms without asking.** Make a contradicting decision and ask
  it to commit. *Expect:* it shows the supersede and waits for you.
- [ ] **9.5 Branch and merge from chat.** "Branch testing as pricing-b, commit this alternative
  there, then merge it into main." *Expect:* it presents the conflict and asks which to keep.
- [ ] **9.6 Errors are readable.** "Show the status of the repo does-not-exist."
  *Expect:* a not-found message, not a crash.
- [ ] **9.7 Handoff to a new chat.** In a fresh chat: "Pull testing with a 2000-token budget and
  summarize where we are." *Expect:* it knows the current decisions without the old chat.
- [ ] **9.8 Cross-model handoff.** Connect ChatGPT the same way (developer mode), and ask it to
  pull for `gpt-5`. *Expect:* Markdown context with the same decisions.
- [ ] **9.9 Prompts.** In clients that show MCP prompts, try *Commit this conversation* and
  *Load context*.
- [ ] **9.10 Raw tool testing.** `npx @modelcontextprotocol/inspector` → Streamable HTTP →
  `$URL/mcp` → Connect → call each tool with JSON arguments.

## 9b. In-chat views (MCP Apps)

In Claude (web or desktop) with the ctx connector re-added after deploying, or locally with
basic-host (see the README's Development section):

- [ ] **9b.1 Commit review.** Ask the model to commit a change that replaces a stored decision.
  *Expect:* a review card with each entry, its action and reason. Untick one, set a flagged
  entry to "Replace the old item", click **Confirm**. The card switches to "Committed" with
  counts, and the model acknowledges what you confirmed.
- [ ] **9b.2 Discard.** Commit again, click **Discard**. *Expect:* "Proposal discarded";
  `ctx status` shows no new commit.
- [ ] **9b.3 Status.** "Show the status of <repo>". *Expect:* branch, counts by type, any pending
  merge with a **Resolve** button, and History / Last change / Pull context buttons.
- [ ] **9b.4 History.** Click **History** (or "show the history"). *Expect:* the commit graph with
  branch lanes; clicking a commit shows its diff and "Show pipeline decisions".
- [ ] **9b.5 Diff.** "What changed since HEAD~2?". *Expect:* the changes list; editing the refs
  and clicking **Compare** updates it.
- [ ] **9b.6 Conflict resolver.** Set up a conflict (testing guide 5.3), then "merge alt into main".
  *Expect:* original / this branch / incoming side by side. Try **Keep incoming**, and on another
  run **Edit…** with your own text. After the last conflict the card says "Merged". **Abort
  merge** cancels instead.
- [ ] **9b.7 Pull and budget.** "Pull <repo>". *Expect:* Claude calls `ctx_pull` without a size
  and says it is waiting; the view shows **Minimum / Recommended / Full** marks and the slider
  starts at Recommended. Dragging updates the preview (kept / shortened / dropped per item)
  without anything reaching Claude. **Load into chat (N tokens)** adds the context and posts
  "Loaded <repo> into the chat"; Claude then answers from it. Changing the size afterwards offers
  **Replace with this size**. Pinning a dropped item keeps it and can raise the minimum.
  Saying "pull <repo> at 800 tokens" loads directly, without waiting.
- [ ] **9b.7b Committing does not pull.** Ask Claude to commit something to a repo with items.
  *Expect:* Claude calls `ctx_stored` (no view appears) and then `ctx_commit`; no pull view.
- [ ] **9b.8 Pin from the CLI.** `ctx pin <item>` and `ctx pin <item> --off`; `ctx items` shows ★.
- [ ] **9b.9 Without MCP Apps.** In a client without Apps support (or the MCP Inspector), the same
  tools still return their full results as text.

## 10. Web viewer

- [ ] **10.0 Your repositories.** The home page lists repos you own under "Owned by you" and
  repos shared with you under "Shared with you" with your role and the owner. After
  `ctx unshare`, the shared repo disappears from the other person's list on reload. Repos you
  have no access to never appear.

- [ ] **10.1 History tab.** The commit graph shows branches as colored lanes and merges joining
  them; branch names label their heads.
- [ ] **10.2 Commit detail.** Click a commit: message, author, client/model, time, and the
  diff (+ added, ~ superseded, ✓ resolved, − retracted).
- [ ] **10.3 Pipeline decisions.** "Show pipeline decisions" lists each audit row.
- [ ] **10.4 State tab.** Switch branches in the dropdown; tick "include closed items".
- [ ] **10.5 Merges tab.** With a pending merge (5.7), both sides of each conflict are shown.
- [ ] **10.6 Members tab.** As owner: share by email, change roles, remove people.
- [ ] **10.7 Mobile layout.** Open on a phone or narrow the window: no horizontal scrolling.
- [ ] **10.8 Dark mode.** Switch your OS to dark mode; the viewer follows it.
- [ ] **10.9 Sign out** from the top bar.

## 11. REST API

- [ ] **11.1 Interactive docs.** Open `$URL/api/docs`.
- [ ] **11.2 Authenticated call.**
  ```powershell
  $t = (Get-Content "$env:APPDATA\ctx\config.json" | ConvertFrom-Json).access_token
  Invoke-RestMethod "$URL/api/v1/repos" -Headers @{Authorization="Bearer $t"}
  ```

## 12. Operations, security and limits

- [ ] **12.1 Health.** `Invoke-RestMethod "$URL/health"` → `status: ok` (proves the
  database connection).
- [ ] **12.2 Rate limiting.** Run 22 heavy requests in under a minute:
  ```powershell
  1..22 | ForEach-Object { ctx --json pull --budget 300 *> $null; "$_ exit=$LASTEXITCODE" }
  ```
  *Expect:* the last ones fail with `rate_limited`; a minute later it works again.
- [ ] **12.3 Security headers.** `(Invoke-WebRequest "$URL/app/").Headers` includes
  `Content-Security-Policy`, `Strict-Transport-Security` and `X-Frame-Options`.
- [ ] **12.4 Privacy page.** `$URL/privacy`.
- [ ] **12.5 Logs.** `gcloud run services logs read ctx --region asia-south1 --limit 50`
- [ ] **12.6 Traces.** Cloud console → Trace explorer: a commit shows spans for the pipeline,
  database queries, embedding calls and NLI checks.
- [ ] **12.7 Database.** Cloud SQL Studio (see the README) → `SELECT * FROM pipeline_audit
  ORDER BY id DESC LIMIT 20;`
- [ ] **12.8 Infrastructure has no drift.** Terraform `plan` reports "No changes".
- [ ] **12.9 Delete is permanent.** `ctx delete-repo testing` → the repo and its history are
  gone; `ctx status` reports not found.

---

## 3b. Comparison scope and item size

- [ ] **3b.1 Many different facts, no flags.** In Claude, describe a project with 8–10
  different facts (model, sensors, cost, privacy…) and ask it to commit to a fresh repo.
  *Expect:* every entry is **add**; nothing is flagged because no two items share a subject.
- [ ] **3b.2 Notes about the chat.** If Claude records something like "Provided a glossary",
  *Expect:* a **flag** tagged "note about the chat", preset to "Don't add".
- [ ] **3b.3 Conflict inside one commit.** Commit
  `[{"type":"decision","subject":"Primary datastore","content":"Use Postgres."},
  {"type":"decision","subject":"Primary datastore","content":"Use MongoDB."}]` with `--no-auto`.
  *Expect:* the second entry is a **flag** against `e1`.
- [ ] **3b.4 Long items are refused.** Commit an item whose content is over 300 characters, or
  whose subject is over 60.
  *Expect:* `validation_failed` asking to split it into separate items / keep the subject to
  2–5 words.

## Benchmarks

The benchmarks run locally with Docker running (they start a throwaway Postgres). The real-model
suites call Vertex AI and use a little trial credit.

```powershell
# Offline (rule-based stand-in, free, deterministic): pipeline, noise and merge logic
uv run python -m evals.run

# Real models: set these once per terminal
$env:GOOGLE_CLOUD_QUOTA_PROJECT="<project-id>"; $env:CTX_GCP_PROJECT="<project-id>"
$env:CTX_EMBEDDING_PROVIDER="vertex"; $env:CTX_NLI_ENABLED="true"

uv run python -m evals.run --real --suites pipeline,noise,merge --n 40
uv run python -m evals.run --real --suites handoff --handoff-cases 6 --budgets 400,800,1600
```

Results are written to `evals/results/*.json`; the handoff suite also prints a table.

| Question | Suite and metric | What good looks like |
|---|---|---|
| Does context transfer between chats and models correctly? | handoff: `probe_accuracy` for `ctx` | Close to the full-transcript ceiling, at a fraction of the tokens |
| Are outdated decisions carried over by mistake? | handoff and pipeline: `superseded_fact_leakage` | Near 0 for ctx; higher for truncated transcripts |
| Is token use improved? | handoff: `mean_context_tokens`, `token_savings_vs_full_transcript`, `accuracy_per_1k_tokens` | Large savings vs the full transcript, higher accuracy per token than the baselines |
| Does ctx beat simpler handoffs at the same budget? | handoff: `ctx` vs `truncated_transcript` vs `self_summary` at each budget | ctx ≥ baselines, especially at small budgets |
| Does the budget get used sensibly? | handoff: `budget_utilisation` | ≤ 1.0 always; well below 1.0 when the state is small |
| Does the pipeline keep the right state? | pipeline: `state_accuracy`, `false_supersession_rate` | High accuracy, near-zero false supersession |
| Are duplicates caught without losing distinct items? | pipeline: `dedupe_precision`, `dedupe_recall` | Both high; real embeddings plus NLI should beat the offline stand-in on paraphrases |
| Does ctx avoid pointless review flags? | noise: `unnecessary_flag_rate`, `same_commit_conflict_recall` | 0 flags on multi-subject commits; every in-commit conflict flagged |
| Are cross-branch conflicts caught? | merge: `conflict_precision`, `conflict_recall` vs latest-wins | High recall; latest-wins catches none |
| Are compatible changes merged correctly? | merge: `auto_merge_correctness` | Near 1.0 |
| How fast is it? | `latency_ms` for commit and pull | Pull well under a second; commit around a second |

The handoff suite builds multi-session project conversations (about 7,000 tokens each, four
sessions) in which decisions are introduced and changed across sessions and buried in
unrelated discussion. A stand-in for the user's chat model (Gemini, run by the benchmark, not
by the ctx server) commits at the end of each session, marking replacements by id as the
commit tool asks; it then answers one probe question per decision from a context of the
given budget. Scoring checks for the latest value; an answer that names an earlier value
instead counts as leakage. Both roles use Gemini (the only model the GCP trial covers), so
"cross-model" here means separate extraction and answering calls rather than two vendors.
