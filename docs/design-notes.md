# Design notes and deviations from the design document

The implementation follows the design document (2026-11-01). Below are the decisions it left
open, and the places where the implementation differs, with the reason for each.

## Open questions, decided

| Question | Decision |
|---|---|
| Product/CLI name | `ctx` (working name; renaming means changing the package names and Terraform `service_name`) |
| Arbiter | **No LLM on the server.** The user's own chat model extracts items and marks replacements; the server judges relations with rules, embeddings and a local NLI model, and flags what it is unsure about. See below. |
| Embedding model | Vertex `gemini-embedding-001` at 768 dimensions. The schema fixes 768, and local `bge-base-en-v1.5` (also 768) works for offline or sensitive use |
| Additive auto-commit | On by default; per repo (`ctx settings --no-auto-commit`) and per call (`auto_confirm`) |
| Web viewer | Built: commit graph, diffs with the pipeline audit, state per branch, pending merges, sharing. Repos are opened by name. Merge resolution stays in chat and the CLI |

## No language model on the server

The design document used an LLM arbiter on the server for relation classification, importance
scoring, compression and export extraction. The implementation removes all of it, so the only
model tokens spent are the user's own chat model (which already reads the conversation) and
embeddings:

| Step | How it is done |
|---|---|
| Extracting items | The user's chat model, guided by the `ctx_commit` tool description. Exports and documents are attached in the chat and committed the same way; there is no server-side import. |
| Marking changes of mind | The chat model sets `replaces` to a stored item's id (shown as `[id:…]` in pulls) and uses `updates` for answered questions and finished tasks. Explicit references always win. |
| Duplicates, refinements, contradictions | Rules in `core/relations.py`: identical text, one statement extending the other, wording that marks a temporary situation, changed numbers or negation, embedding similarity, single-valued decisions on the same subject, and a local NLI cross-encoder run in both directions (int8 ONNX build of `cross-encoder/nli-deberta-v3-small`, baked into the image). Verdicts below `CTX_MIN_CONFIDENCE` are flagged for the user. |
| Open question answered | A decision or fact on the same subject resolves it; weaker matches are not applied. |
| Importance | Type weight, recency and use count. |
| Compressed form | Deterministic per type: decisions keep the choice and a short reason, facts one clause, constraints and open questions verbatim. |

Measured with real embeddings and NLI (30 cases per suite): pipeline state accuracy 0.90 (the
remaining cases are flagged, not guessed), duplicate precision 1.00 and recall 0.84, merge
conflict recall 1.00 at precision 0.92, and a median commit time of 0.66 s, against 2–17 s with
the earlier server-side LLM.

## Deviations

- **Repo names are globally unique** (case-insensitive), so a repo is addressed by its name
  alone and there is no `owner/name` form. Creating a taken name says so, which reveals that
  the name exists; membership and contents stay private.
- **No repository listing.** People open repos by name (the web viewer remembers recently
  opened ones in the browser).
- **Supersession links live on `commit_items`, not `state_items`.** An item can be superseded
  on one branch and still active on another, so the link belongs to a snapshot.
  `state_items` stays immutable, apart from provenance (sources a duplicate was folded into) and
  a use counter for importance scoring.
- **Extra tables** the design implies but didn't list: `proposals` (pending diffs),
  `user_heads` (per-user checkout), `oauth_*` (authorization server), `rate_limits`.
- **Comparison across types.** Goals, constraints, decisions and facts are compared with each
  other, since the same thing is often recorded as a decision in one session and a fact in
  the next. Tasks and open questions are compared only with their own kind.
- **Near-duplicates that differ in numbers or negation** ("region is asia-south1" vs
  "us-central1") are never dropped on similarity alone; they are treated as contradictions.
- **Same reversal on both branches.** When both branches supersede the same item, the two
  replacements are compared: an identical change merges cleanly, and a different change is one
  conflict, not two.
- **CLI login** uses the browser with a loopback redirect (RFC 8252), not the device flow. It
  works with the MCP SDK's standard OAuth endpoints and needs no extra grant type.
- **Claude token counting** is an o200k count multiplied by a safety factor unless
  `CTX_ANTHROPIC_API_KEY` is set. The manifest reports whether the count is exact. GPT counts
  use tiktoken and are always exact.
- **Row-level security** is not used. Authorization is one service-layer function covered by
  tests (the design listed RLS as "consider").
- **Unresolved conflicts in renders.** While a merge into a branch is pending, renders of that
  branch include a warning per open conflict (the "unresolved-conflict warnings" tier).
- **In-chat views** are one MCP Apps resource (`ui://ctx/app.html`), a single self-contained
  React bundle. Tools tag their results with a `view` name; helper tools the views need
  (`ctx_app_show`, `ctx_app_merge`, `ctx_app_discard`) are visible to the app only. Buttons call
  the same tools the model uses and report the user's decision to the model.
- **Pinning** is a setting on the item, not a commit: it changes render priority (pinned items
  are compressed if needed but never silently dropped) and is recorded in the audit log.
- **NLI refinement needs strong evidence.** One-way implication counts as a refinement only at
  0.9 or above, because a different choice can imply the old one in general terms
  ("Postgres with pgvector" implies "a vector DB" yet replaces "a separate vector DB").
- **A refinement on one branch against a replacement on the other** is one conflict between the
  two current versions; resolving it closes the losing side's replacement as well.

## Verify before relying on (as the design asks)

- ChatGPT's current support for write-capable custom MCP connectors.
- Claude's custom connector requirements. The server implements dynamic client registration,
  PKCE, protected-resource metadata at both `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-protected-resource/mcp`, and token revocation.
- The pgvector version on Cloud SQL. HNSW needs 0.5.0 or later; Cloud SQL for PostgreSQL 16
  ships a newer version.
