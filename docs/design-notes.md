# Design notes and deviations from the design document

The implementation follows the design document (2026-11-01). Below are the decisions it left
open, and the places where the implementation differs, with the reason for each.

## Open questions, decided

| Question | Decision |
|---|---|
| Product/CLI name | `ctx` (working name; renaming means changing the package names and Terraform `service_name`) |
| Arbiter | Gemini on Vertex AI by default (covered by GCP trial credits). Claude on Vertex and the Gemini API are selectable by config |
| Embedding model | Vertex `gemini-embedding-001` at 768 dimensions. The schema fixes 768, and local `bge-base-en-v1.5` (also 768) works for offline or sensitive use |
| Additive auto-commit | On by default; per repo (`ctx settings --no-auto-commit`) and per call (`auto_confirm`) |
| Web viewer | Built: commit graph, diffs with the pipeline audit, state per branch, pending merges, sharing. Merge resolution stays in chat and the CLI |

## Deviations

- **Supersession links live on `commit_items`, not `state_items`.** An item can be superseded
  on one branch and still active on another, so the link belongs to a snapshot.
  `state_items` stays immutable, apart from provenance (sources a duplicate was folded into) and
  a use counter for importance scoring.
- **Extra tables** the design implies but didn't list: `proposals` (pending diffs),
  `user_heads` (per-user checkout), `jobs` (background work), `oauth_*` (authorization server),
  `rate_limits`.
- **Questions closed by answers.** The relation taxonomy covers item-to-item relations of the
  same type. Closing an open question when a decision or fact answers it uses a separate
  arbiter check ("does this answer the question?"), shown in the diff as a `resolve` entry.
- **Near-duplicates that differ in numbers or negation** ("region is asia-south1" vs
  "us-central1") are never dropped on similarity alone. They go to the arbiter, because
  embeddings rate them as almost identical.
- **Same reversal on both branches.** When both branches supersede the same item, the two
  replacements are compared by meaning: an identical change merges cleanly, and a different
  change is one conflict, not two.
- **CLI login** uses the browser with a loopback redirect (RFC 8252), not the device flow. It
  works with the MCP SDK's standard OAuth endpoints and needs no extra grant type.
- **NLI prefilter** is supported (`CTX_NLI_ENABLED`, image built with `WITH_NLI=true`) but
  off by default: PyTorch adds about 1 GB to the image and slows Cloud Run cold starts. Without
  it, cheap similarity and number/negation checks decide which pairs reach the arbiter.
- **Claude token counting** is an o200k count multiplied by a safety factor unless
  `CTX_ANTHROPIC_API_KEY` is set. The manifest reports whether the count is exact. GPT counts
  use tiktoken and are always exact.
- **Row-level security** is not used. Authorization is one service-layer function covered by
  tests (the design listed RLS as "consider").
- **Unresolved conflicts in renders.** While a merge into a branch is pending, renders of that
  branch include a warning per open conflict (the "unresolved-conflict warnings" tier).

## Verify before relying on (as the design asks)

- Current Gemini model names, and their availability in `vertex_location`.
- ChatGPT's current support for write-capable custom MCP connectors.
- Claude's custom connector requirements. The server implements dynamic client registration,
  PKCE, protected-resource metadata at both `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-protected-resource/mcp`, and token revocation.
- The pgvector version on Cloud SQL. HNSW needs 0.5.0 or later; Cloud SQL for PostgreSQL 16
  ships a newer version.
