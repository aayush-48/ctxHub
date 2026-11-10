# ctx: git for LLM context

ctx gives LLM conversations git-style version control. Instead of saving raw chat turns, it
stores the **task state** of the work, as typed items:

| Type | Example |
|---|---|
| goal | Ship a CLI MVP before the next sprint |
| constraint | Must run on GCP; no paid vector DB |
| decision | Use Postgres + pgvector over a separate vector DB |
| open_question | Does ChatGPT allow write tools over MCP? |
| fact | Cloud Run region is asia-south1 |
| task | Write the merge-base query |

You **commit** state from a chat in Claude or ChatGPT, **branch** to explore alternatives,
**merge** branches by meaning (contradictions always go to you), and **pull** the state into
another chat or model within a token budget.

People use it from the chat apps they already have (through MCP), from a terminal (the `ctx`
CLI), and from a web viewer.

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Run it locally](#run-it-locally)
- [Deploy](#deploy)
- [Connect a chat app](#connect-a-chat-app)
- [CLI](#cli)
- [Web viewer](#web-viewer)
- [REST API](#rest-api)
- [Configuration](#configuration)
- [Development](#development)
- [Evaluation](#evaluation)
- [Security and privacy](#security-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Status and roadmap](#status-and-roadmap)

## How it works

- **Commits are cleaned before they are stored.** Each commit runs through a pipeline: validate →
  normalize → embed → dedupe → classify relations → supersede → score importance → compress.
  The server never calls a language model: your own chat model (Claude, ChatGPT…) extracts the
  items, and the server checks them with embeddings, rules and a small local NLI model,
  flagging anything it is unsure about for you. The result is a *proposed diff*. Nothing is written until you confirm it, except
  diffs that only add items, which can commit automatically.
- **Items are compared only when they answer the same question.** Each item has a short
  subject (2–5 words, at most 60 characters) and one fact as content (at most 300 characters);
  longer items are rejected with a message asking the chat model to split them. Two items are
  checked for duplicates, refinements or contradictions only when their subjects match (same
  text, or subject embeddings at cosine ≥ 0.85), so a commit of many different facts about one
  project raises no flags. Items inside the same commit are compared too: a duplicate is
  dropped, a more detailed version replaces the shorter one, and a real conflict is flagged
  (order inside one commit says nothing about which came later). Items that describe the chat
  itself ("Provided a glossary", "User may want…") are flagged as a note about the chat, preset
  to "Don't add".
- **History is never rewritten.** A replaced item is closed and linked to its replacement, and a
  revert is itself a new commit. Every pipeline decision is recorded in an audit log.
- **Merges are three-way and semantic.** Changes made on only one side apply directly.
  Duplicates are folded together. Contradictions become conflicts that you resolve as ours,
  theirs, both, or an edited item. Timestamps never settle disagreements between branches,
  because a later commit on another branch is not a later decision on this one.
- **Renders fit a token budget you choose.** Every pull reports three sizes: minimum
  (constraints and pinned items), recommended (every item, shortened where possible) and full.
  Without a budget, the recommended size is used. Items go in priority order (constraints and
  goals, conflict warnings, decisions, open questions, tasks, facts); every item is shortened
  before any is dropped, and a manifest lists everything left out. Constraints are never dropped
  silently.
  Claude gets XML-style sections, GPT gets Markdown.

A typical flow:

```
Claude chat ──ctx_commit──▶ pipeline ──▶ proposed diff ──you confirm──▶ commit on main
                                                                           │
ChatGPT chat ◀──ctx_pull (budget 3000, GPT template)───────────────────────┘
```

## Architecture

One stateless Cloud Run service serves every interface. The version engine is the only code
that writes commits, and Postgres is the single source of truth.

```
 Claude / ChatGPT        CLI            Browser
   (MCP client)       (REST + OAuth)   (web viewer)
        │                  │                │
        └──────────────────┼────────────────┘
                           ▼
        ┌────────────── Cloud Run: ctx-server ──────────────┐
        │  /mcp (MCP)  /api/v1 (REST)  /app (viewer)        │
        │  OAuth 2.1 server (/authorize /token /register)   │
        │  service layer (auth checks) → pipeline / merge / │
        │  renderer → version engine                        │
        └───┬───────────────┬───────────────┬───────────────┘
            ▼               ▼               ▼
     Cloud SQL Postgres     Vertex AI          local NLI model
     + pgvector             (embeddings only)  (ONNX, in the container;
     (private IP)                              no API calls)
```

| Layer | Technology |
|---|---|
| Server | Python 3.12, FastAPI, Pydantic v2, official MCP Python SDK (Streamable HTTP) |
| Database | PostgreSQL 16 + pgvector (HNSW), SQLAlchemy 2 (async), Alembic |
| AI | Vertex AI `gemini-embedding-001` (768 dims) for embeddings; a local int8 ONNX build of `cross-encoder/nli-deberta-v3-small` for entailment and contradiction checks. No generative model on the server. |
| Auth | OAuth 2.1 with PKCE and dynamic client registration; Google or GitHub sign-in |
| CLI | Typer, httpx, Rich |
| Web viewer | React 18, TypeScript, Vite |
| Infra | Cloud Run, Cloud SQL, Secret Manager, Artifact Registry, Terraform |
| Observability | Structured JSON logs (Cloud Logging), OpenTelemetry traces (Cloud Trace) |
| CI/CD | GitHub Actions, keyless deploys via Workload Identity Federation |

Every AI provider can be swapped by configuration: Vertex AI, the Gemini API with an AI
Studio key, local sentence-transformers, or offline fakes for tests.

## Repository layout

```
server/      FastAPI app: REST API, MCP server, OAuth server, pipeline, merge, renderer
  src/ctx_server/core/        version engine: pipeline, relation rules, merge, render, graph
  src/ctx_server/providers/   embeddings, local NLI model, tokenizers, retries
  src/ctx_server/auth/        OAuth provider and sign-in pages
  src/ctx_server/migrations/  database schema (Alembic)
  tests/                      unit, database and HTTP end-to-end tests
cli/         `ctx` command-line client
web/         web viewer (built into the server image)
infra/       Terraform for Google Cloud
evals/       benchmarks: pipeline, merge, handoff fidelity
examples/    sample item files used in the guides
docs/        deployment guide and design notes
```

## Prerequisites

| For | You need |
|---|---|
| Running locally | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| CLI and development | [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you) |
| Web viewer development | Node.js 20+ |
| Deploying | A Google Cloud project with billing (the free trial works) and the [gcloud CLI](https://cloud.google.com/sdk/docs/install). Terraform can run in Docker, so no install is needed. |

## Run it locally

This starts Postgres and the server with offline stand-ins for the AI models (rule-based, no
cloud needed) and a development sign-in that accepts any email:

```bash
docker compose up --build
# open http://localhost:8080
```

Use the CLI against it:

```bash
uv tool install ./cli            # or: pipx install ./cli
ctx login http://localhost:8080
ctx init demo
ctx commit -m "Initial plan" -f examples/state.json
ctx commit -m "Switch datastore" -f examples/change-datastore.json
ctx log
ctx pull --model claude --budget 400
```

Stop it with `docker compose down`.

## Deploy

[docs/deploy-gcp.md](docs/deploy-gcp.md) has step-by-step instructions for a GCP project,
including the free trial. In short:

1. Create a project and a Google OAuth client.
2. `terraform apply` in `infra/`, which creates about 47 resources.
3. Build the image and `gcloud run deploy`, or push to `main` with GitHub deploys set up.

## Connect a chat app

The MCP endpoint is `https://ctx-910037083701.asia-south1.run.app/mcp`.

- **Claude:** Settings → Connectors → *Add custom connector* → paste the URL → sign in and allow.
- **ChatGPT:** Settings → Apps & Connectors → developer mode → create a connector with the URL.
  Check ChatGPT's current rules for custom connectors with write tools; if your plan doesn't
  allow them, use `ctx pull --copy` and paste the context instead.

Then ask the model things like:

- "Create a ctx repo called *launch-plan*."
- "Commit the decisions we've made so far to launch-plan."
- "Branch launch-plan as *pricing-b* and commit this alternative there."
- "Merge pricing-b into main." The model shows you any conflicts and asks how to resolve them.
- In a new chat or another model: "Pull launch-plan" (pick a size in the view), or "Pull launch-plan at 1500 tokens".

| MCP tool | Does |
|---|---|
| `ctx_stored` | Called before every commit: one short line per stored item (id, type, subject) plus the rules for writing items. No view |
| `ctx_commit`, `ctx_confirm` | Propose a commit, then write it after you agree |
| `ctx_pull` | Load the state into the chat. Without a size, the user picks one in the view first (minimum / recommended / full) and the model gets nothing until then; with `token_budget`, the context loads directly |
| `ctx_inspect` (read-only) | `status`, `log`, `diff` |
| `ctx_manage` | `init` (names are globally unique), `branch`, `checkout`, `merge`, `merge_complete`, `merge_abort`, `resolve`, `revert` (a new commit), `pin` (no commit) |

**Token cost.** Everything the chat model reads is kept small, and CI checks the sizes
(`server/tests/test_token_budget.py`). The six tool definitions total about 1,500 tokens per
message. The rules for writing items (about 350 tokens) load only when committing, inside the
`ctx_stored` result. Results are compact: a 16-item commit comes back in about 260 tokens, one
line per entry. Chat pulls leave out item ids. The in-chat views fetch full data through
view-only helpers the model never sees.

### In-chat views (MCP Apps)

In clients that support [MCP Apps](https://modelcontextprotocol.io/specification/draft/extensions/apps)
(Claude on the web and desktop, ChatGPT, VS Code), these tools open an interactive view inside the chat:

| View | Opens on | What you can do |
|---|---|---|
| Commit review | `ctx_commit` | Untick entries, choose keep/replace/skip for flagged ones, Confirm or Discard |
| Status | `ctx_inspect` status | Counts by type, pending merges (open the resolver), jump to history or pull |
| History | `ctx_inspect` log | Commit graph with branches and merges; click a commit for its diff and pipeline decisions |
| Diff | `ctx_inspect` diff | Changes between two refs; pick other refs to compare |
| Conflict resolver | `ctx_manage` merge / resolve | Original, this branch and incoming side by side; keep either, both, or edit; complete or abort |
| Pull and budget | `ctx_pull` | Slider from **Minimum** (constraints and pinned items) through **Recommended** (every item, shortened where possible) to **Full**; live preview of what is kept, shortened or dropped; pin/unpin; **Load into chat** sends the chosen size |

Every button calls the same tools, so the model stays informed: the view also tells the model what
you decided. Clients without MCP Apps get the same results as text.

The server also provides two MCP prompts, *Commit this conversation* and *Load context*, for
clients that show MCP prompts.

## CLI

Install it from this repository:

```bash
pipx install "git+https://github.com/aayush-48/ctxHub.git#subdirectory=cli"
# or, from a clone: uv tool install ./cli
ctx login https://<your-service>
```

| Command | Does |
|---|---|
| `ctx login [URL]`, `ctx logout`, `ctx whoami` | Browser sign-in (OAuth + PKCE, loopback redirect) |
| `ctx init <repo>`, `ctx use <repo>` | Create a repo (names are globally unique) and pick the default repo |
| `ctx commit -m <msg> -f items.json [--yes] [--no-auto]` | Commit items from a file; shows the diff and asks to confirm |
| `ctx confirm <proposal> [--accept e1,e2] [--choice e3=drop]` | Confirm a pending proposal |
| `ctx status`, `ctx items [--all]` | Current branch, counts, items |
| `ctx log [--all] [-n N]`, `ctx diff <a> [b]`, `ctx show [ref] [--audit]` | History, diffs, pipeline decisions |
| `ctx branch [name] [--from ref] [-d]`, `ctx checkout <ref>` | Branch and move |
| `ctx merge <branch> [--review]`, `ctx merge --continue`, `ctx merge --abort` | Merge; conflicts are resolved interactively |
| `ctx resolve <conflict> ours\|theirs\|both\|edited` | Resolve one conflict without prompts |
| `ctx pull --model claude --budget 4000 [--copy] [-o file]` | Render context for a model |
| `ctx pin <item> [--off]` | Pin an item so renders never drop it |
| `ctx revert <commit>` | Undo a commit with a new commit |
| `ctx share <email> --role editor`, `ctx members`, `ctx unshare <email>` | Multi-user access |
| `ctx settings [--no-auto-commit] [--no-auto-merge]` | Repo settings |
| `ctx delete-repo <repo>` | Permanent delete of a repo and its history |

Every command accepts `--json`. Refs work like git: branch names, commit-id prefixes,
`HEAD~2`, `main^2`. An item file is a JSON list of `{type, subject, content, reason?}`
(see [examples/](examples/)).

## Web viewer

Open the service URL and sign in. The home page lists **your repositories** (owned by you) and
**repositories shared with you** (with your role, viewer or editor, and the owner), most
recently updated first. Nobody sees repositories they have no access to. Each repository has
four tabs:

- **History:** the commit graph with branches and merges. Click a commit to see its diff and
  the pipeline's decisions.
- **State:** the items on any branch, optionally including superseded and resolved ones.
- **Merges:** pending merges, with both sides of each conflict.
- **Members:** who has access; owners can share and remove people.

## REST API

The CLI and the viewer use a REST API under `/api/v1`. Browse it interactively at
`https://<your-service>/api/docs`. Every request needs `Authorization: Bearer <token>`, which
`ctx login` stores in your CLI config.

## Configuration

The server reads environment variables prefixed `CTX_`. In production Terraform sets them;
locally, copy [.env.example](.env.example) to `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `CTX_ENV` | `dev` | `dev`, `test` or `prod` (prod refuses insecure settings) |
| `CTX_PUBLIC_BASE_URL` | `http://localhost:8080` | Public URL; used for OAuth and links |
| `CTX_DATABASE_URL` | local Postgres | `postgresql+asyncpg://…` |
| `CTX_SESSION_SECRET` | dev value | Signs login cookies; required in prod |
| `CTX_GOOGLE_CLIENT_ID` / `_SECRET` | empty | Enables Google sign-in |
| `CTX_GITHUB_CLIENT_ID` / `_SECRET` | empty | Enables GitHub sign-in |
| `CTX_DEV_LOGIN` | `false` | Development sign-in with any email; refused in prod |
| `CTX_EMBEDDING_PROVIDER` | `vertex` | `vertex`, `gemini_api`, `local` or `fake` |
| `CTX_GCP_PROJECT` / `CTX_VERTEX_LOCATION` | empty / `us-central1` | Vertex AI project and embedding region |
| `CTX_GEMINI_API_KEY` | empty | For `CTX_EMBEDDING_PROVIDER=gemini_api` (no GCP needed) |
| `CTX_NLI_ENABLED` | `false` (`true` in the container) | Local NLI model for entailment/contradiction checks |
| `CTX_NLI_MODEL_DIR` | empty (`/srv/models/nli` in the container) | Pre-downloaded model files; otherwise fetched from Hugging Face once |
| `CTX_MIN_CONFIDENCE` | `0.7` | Below this, a relation is flagged for the user instead of applied |
| `CTX_ANTHROPIC_API_KEY` | empty | Exact Claude token counts (otherwise a safe estimate) |

Pipeline thresholds, importance weights and rate limits are also configurable; see
[server/src/ctx_server/config.py](server/src/ctx_server/config.py).

## Development

```bash
uv sync                                   # install everything (Python 3.12)
uv run pytest                             # all tests; needs Docker (Testcontainers Postgres)
uv run ruff check server cli evals        # lint
uv run ruff format server cli evals       # format
```

Web viewer with hot reload, against a server on port 8080:

```bash
cd web && npm install && npm run dev      # http://localhost:5173/app/
```

The in-chat views live in `web/src/mcp/` and build to one self-contained HTML file served as
`ui://ctx/app.html` (`npm run build` builds both the viewer and the views). To try them locally,
run the server with the development sign-in, put the dev-only auth proxy in front of it, and
point the official [basic-host](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples/basic-host)
test host at the proxy:

```bash
CTX_DEV_LOGIN=true CTX_EMBEDDING_PROVIDER=fake CTX_NLI_ENABLED=true   uv run uvicorn ctx_server.app:app --port 8090
uv run python server/scripts/dev_mcp_proxy.py --server http://127.0.0.1:8090 --port 3001
# in a clone of modelcontextprotocol/ext-apps, examples/basic-host:
SERVERS='["http://localhost:3001/mcp"]' npm start      # then open http://localhost:8080
```

Database changes go in a new Alembic migration under
`server/src/ctx_server/migrations/versions/`. The app applies migrations on startup.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests, lint, the web
build, Terraform validation, an image build and the offline evals with quality gates on every
push.

## Evaluation

**Continuing a project across chats:** [docs/benchmark-handoff-chain.md](docs/benchmark-handoff-chain.md)
covers a 5-chat planning project handed over by summary, by ctx and by full transcript. After
four handoffs, the summary method answered 50% of questions correctly, ctx 88% and the full
transcript 91%. ctx used 1.9× the summary's tokens and 30% fewer than the full transcript.

```bash
uv run python -m evals.run                          # offline: pipeline + noise + merge
uv run python -m evals.run --real --suites all      # real models, incl. handoff fidelity
```

| Suite | Measures | Compared against |
|---|---|---|
| pipeline | state accuracy, superseded-fact leakage, false-supersession rate, dedupe precision/recall | rules with and without the NLI model |
| noise | unnecessary-flag rate on commits of many different subjects (one commit and across commits); recall on two conflicting values inside one commit | no flags at all; every in-commit conflict flagged |
| merge | conflict precision/recall, auto-merge correctness | naive "latest wins" |
| handoff | probe-question accuracy after commit in model A and pull in model B; accuracy per 1k tokens | truncated raw transcript, model self-summary |

Results with real embeddings and the local NLI model (`gemini-embedding-001`, 30 cases per suite; no server-side LLM):

| | Result | Baseline |
|---|---|---|
| Pipeline state accuracy | 0.90; the rest are flagged for the user rather than guessed | |
| Duplicate detection | precision 1.00, recall 0.84 | |
| Unnecessary flags on multi-subject commits (incl. a real 16-item project commit with closely related subjects) | 0 of 332 entries | 9 with the earlier subject threshold of 0.85 |
| Conflicts inside one commit flagged | 1.00 | |
| Merge conflicts found | precision 0.92, recall 1.00 | latest-wins: 0, every conflict silently overwritten |
| Auto-merge correctness | 0.99 | |
| Commit latency | 0.66 s median | 2–17 s with a server-side LLM (earlier design) |

The handoff suite still uses short synthetic conversations that fit in the budget whole, so
it cannot yet show ctx's advantage over a truncated transcript. Longer, multi-session
scenarios are on the roadmap.

## Security and privacy

- OAuth 2.1 with PKCE and dynamic client registration. Tokens are opaque and stored only as
  hashes. Refresh tokens rotate, and reusing an old one revokes the whole token family.
- Roles (owner, editor, viewer) are checked by a single service-layer function that every
  REST endpoint and MCP tool goes through. Non-members get "not found", so repo names don't
  leak.
- Stored text is treated as data. Renders escape it inside clear delimiters, and stored
  content never triggers tool calls. The server runs no generative model, so stored text
  cannot steer it.
- Per-user rate limits (stored in Postgres, so they hold across instances) and payload size
  caps.
- Cloud SQL is on a private IP only, secrets live in Secret Manager, and deleting a repo
  hard-deletes its history and audit rows.
- Item text is sent to Vertex AI only to compute embeddings; relation checks run locally.
  The privacy notice is served at `/privacy`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `gcloud` / `ctx` is not recognized | Open a new terminal (or restart VS Code) after installing; run `uv tool update-shell` for `ctx` |
| Google sign-in says access is blocked | While the OAuth app is in Testing mode, add the account under **Audience → Test users** |
| Vertex AI calls from your PC fail with 404 or permission errors | Set `GOOGLE_CLOUD_QUOTA_PROJECT=<project-id>` |
| `/healthz` returns Google's 404 page | That path is reserved on Cloud Run's public front end; use `/health` |
| `ctx` says the session expired | Run `ctx login <url>` again |
| A commit says the branch moved | Someone committed first; run the commit again so the pipeline compares against the new head |

More detail: [docs/design-notes.md](docs/design-notes.md) covers decisions and deviations from
the design document.

## Status and roadmap

**Working today:** typed commits with the cleaning pipeline, diff and confirm, branch, semantic
merge with conflict resolution, revert, budgeted cross-model pull, multi-user
roles, MCP for Claude and ChatGPT, CLI, web viewer, deployment on GCP, CI and evals.

**Next:**
- Longer, multi-session handoff benchmarks.
- Resolving merge conflicts from the web viewer.
- An optional free-tier deployment (Neon + Hugging Face Spaces + Gemini API key).

---

Made with ❤️ by Aayush
