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
  normalize → embed → dedupe → classify relations (LLM arbiter) → supersede → score importance →
  compress. The result is a *proposed diff*. Nothing is written until you confirm it, except
  diffs that only add items, which can commit automatically.
- **History is never rewritten.** A replaced item is closed and linked to its replacement, and a
  revert is itself a new commit. Every pipeline decision is recorded in an audit log.
- **Merges are three-way and semantic.** Changes made on only one side apply directly.
  Duplicates are folded together. Contradictions become conflicts that you resolve as ours,
  theirs, both, or an edited item. Timestamps never settle disagreements between branches,
  because a later commit on another branch is not a later decision on this one.
- **Renders fit a token budget.** Constraints and goals go in first, then unresolved conflict
  warnings, decisions, open questions, tasks and facts. Items are compressed before they are
  dropped, and a manifest lists everything left out. Constraints are never dropped silently.
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
     Cloud SQL Postgres   Vertex AI      Cloud Storage + Cloud Tasks
     + pgvector           (embeddings,   (export uploads, background
     (private IP)         Gemini)        import jobs)
```

| Layer | Technology |
|---|---|
| Server | Python 3.12, FastAPI, Pydantic v2, official MCP Python SDK (Streamable HTTP) |
| Database | PostgreSQL 16 + pgvector (HNSW), SQLAlchemy 2 (async), Alembic |
| AI | Vertex AI `gemini-embedding-001` (768 dims) and `gemini-3-flash-preview` as arbiter |
| Auth | OAuth 2.1 with PKCE and dynamic client registration; Google or GitHub sign-in |
| CLI | Typer, httpx, Rich |
| Web viewer | React 18, TypeScript, Vite |
| Infra | Cloud Run, Cloud SQL, Secret Manager, Cloud Storage, Cloud Tasks, Artifact Registry, Terraform |
| Observability | Structured JSON logs (Cloud Logging), OpenTelemetry traces (Cloud Trace) |
| CI/CD | GitHub Actions, keyless deploys via Workload Identity Federation |

Every AI provider can be swapped by configuration: Vertex AI, the Gemini API with an AI
Studio key, local sentence-transformers, or offline fakes for tests.

## Repository layout

```
server/      FastAPI app: REST API, MCP server, OAuth server, pipeline, merge, renderer
  src/ctx_server/core/        version engine: pipeline, merge, render, graph, imports
  src/ctx_server/providers/   embeddings, LLM arbiter, NLI, tokenizers, storage
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

The MCP endpoint is `https://<your-service>/mcp`.

- **Claude:** Settings → Connectors → *Add custom connector* → paste the URL → sign in and allow.
- **ChatGPT:** Settings → Apps & Connectors → developer mode → create a connector with the URL.
  Check ChatGPT's current rules for custom connectors with write tools; if your plan doesn't
  allow them, use `ctx pull --copy` and paste the context instead.

Then ask the model things like:

- "Create a ctx repo called *launch-plan*."
- "Commit the decisions we've made so far to launch-plan."
- "Branch launch-plan as *pricing-b* and commit this alternative there."
- "Merge pricing-b into main." The model shows you any conflicts and asks how to resolve them.
- In a new chat or another model: "Pull launch-plan with a 3000-token budget."

| MCP tool | Does |
|---|---|
| `ctx_repos`, `ctx_init` | List and create repositories |
| `ctx_status`, `ctx_log`, `ctx_diff` | Inspect state and history |
| `ctx_commit`, `ctx_confirm` | Propose a commit, then write it after you agree |
| `ctx_branch`, `ctx_checkout` | Branch and move |
| `ctx_merge` (start / complete / abort), `ctx_resolve` | Semantic merge and conflict resolution |
| `ctx_pull` | Render state for a model within a token budget |
| `ctx_revert` | Undo a commit with a new commit |

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
| `ctx repos`, `ctx init <repo>`, `ctx use <repo>` | List, create, and pick the default repo |
| `ctx commit -m <msg> -f items.json [--yes] [--no-auto]` | Commit items from a file; shows the diff and asks to confirm |
| `ctx confirm <proposal> [--accept e1,e2] [--choice e3=drop]` | Confirm a pending proposal |
| `ctx import <export.zip> [-c <conversation>]` | Extract state from a Claude or ChatGPT data export |
| `ctx status`, `ctx items [--all]` | Current branch, counts, items |
| `ctx log [--all] [-n N]`, `ctx diff <a> [b]`, `ctx show [ref] [--audit]` | History, diffs, pipeline decisions |
| `ctx branch [name] [--from ref] [-d]`, `ctx checkout <ref>` | Branch and move |
| `ctx merge <branch> [--review]`, `ctx merge --continue`, `ctx merge --abort` | Merge; conflicts are resolved interactively |
| `ctx resolve <conflict> ours\|theirs\|both\|edited` | Resolve one conflict without prompts |
| `ctx pull --model claude --budget 4000 [--copy] [-o file]` | Render context for a model |
| `ctx revert <commit>` | Undo a commit with a new commit |
| `ctx share <email> --role editor`, `ctx members`, `ctx unshare <email>` | Multi-user access |
| `ctx settings [--no-auto-commit] [--no-auto-merge] [--no-llm]` | Repo settings |
| `ctx delete-repo <repo>` | Permanent delete of a repo, its history and uploads |

Every command accepts `--json`. Refs work like git: branch names, commit-id prefixes,
`HEAD~2`, `main^2`. An item file is a JSON list of `{type, subject, content, reason?}`
(see [examples/](examples/)).

## Web viewer

Open the service URL and sign in. Each repository has four tabs:

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
| `CTX_ARBITER_PROVIDER` | `vertex_gemini` | `vertex_gemini`, `vertex_claude`, `gemini_api`, `none` or `fake` |
| `CTX_ARBITER_MODEL` / `CTX_ARBITER_LOCATION` | `gemini-3-flash-preview` / `global` | Model that classifies relations |
| `CTX_GCP_PROJECT` / `CTX_VERTEX_LOCATION` | empty / `us-central1` | Vertex AI project and embedding region |
| `CTX_GEMINI_API_KEY` | empty | For the `gemini_api` providers (no GCP needed) |
| `CTX_STORAGE_PROVIDER` / `CTX_GCS_BUCKET` | `db` | Where uploaded exports go: `gcs` or the database |
| `CTX_TASKS_PROVIDER` | `inline` | `cloud_tasks` in production |
| `CTX_NLI_ENABLED` | `false` | Optional NLI prefilter (image built with `WITH_NLI=true`) |
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

Database changes go in a new Alembic migration under
`server/src/ctx_server/migrations/versions/`. The app applies migrations on startup.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the tests, lint, the web
build, Terraform validation, an image build and the offline evals with quality gates on every
push.

## Evaluation

```bash
uv run python -m evals.run                          # offline: pipeline + merge
uv run python -m evals.run --real --suites all      # real models, incl. handoff fidelity
```

| Suite | Measures | Compared against |
|---|---|---|
| pipeline | state accuracy, superseded-fact leakage, false-supersession rate, dedupe precision/recall | the same pipeline without the LLM arbiter |
| merge | conflict precision/recall, auto-merge correctness | naive "latest wins" |
| handoff | probe-question accuracy after commit in model A and pull in model B; accuracy per 1k tokens | truncated raw transcript, model self-summary |

First results with real models (Vertex AI, small sample):

| | Result | Baseline |
|---|---|---|
| Pipeline state accuracy | 1.00, no outdated facts kept | 0.25 without the LLM arbiter |
| Merge conflicts found | precision 1.00, recall 0.89 | latest-wins: 0, every conflict silently overwritten |
| Auto-merge correctness | 0.97 | |

The handoff suite still uses short synthetic conversations that fit in the budget whole, so
it cannot yet show ctx's advantage over a truncated transcript. Longer, multi-session
scenarios are on the roadmap.

## Security and privacy

- OAuth 2.1 with PKCE and dynamic client registration. Tokens are opaque and stored only as
  hashes. Refresh tokens rotate, and reusing an old one revokes the whole token family.
- Roles (owner, editor, viewer) are checked by a single service-layer function that every
  REST endpoint and MCP tool goes through. Non-members get "not found", so repo names don't
  leak.
- Stored text is treated as data. Renders escape it inside clear delimiters, the arbiter is
  told never to follow instructions found in items, and stored content never triggers tool
  calls.
- Per-user rate limits (stored in Postgres, so they hold across instances) and payload size
  caps.
- Cloud SQL is on a private IP only, secrets live in Secret Manager, uploads expire after 30
  days, and deleting a repo hard-deletes its history, audit rows and uploads.
- Item text is sent to Vertex AI for embeddings and classification. Owners can turn the LLM
  off per repo (`ctx settings --no-llm`); judgment calls are then flagged for the user.
  The privacy notice is served at `/privacy`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `gcloud` / `ctx` is not recognized | Open a new terminal (or restart VS Code) after installing; run `uv tool update-shell` for `ctx` |
| Google sign-in says access is blocked | While the OAuth app is in Testing mode, add the account under **Audience → Test users** |
| Vertex AI calls from your PC fail with 404 or permission errors | Set `GOOGLE_CLOUD_QUOTA_PROJECT=<project-id>` |
| A Gemini model returns 404 | Model names change; check which ones your project can use and set `CTX_ARBITER_MODEL` / `CTX_ARBITER_LOCATION` |
| `/healthz` returns Google's 404 page | That path is reserved on Cloud Run's public front end; use `/health` |
| `ctx` says the session expired | Run `ctx login <url>` again |
| A commit says the branch moved | Someone committed first; run the commit again so the pipeline compares against the new head |

More detail: [docs/design-notes.md](docs/design-notes.md) covers decisions and deviations from
the design document.

## Status and roadmap

**Working today:** typed commits with the cleaning pipeline, diff and confirm, branch, semantic
merge with conflict resolution, revert, budgeted cross-model pull, export import, multi-user
roles, MCP for Claude and ChatGPT, CLI, web viewer, deployment on GCP, CI and evals.

**Next:**
- Longer, multi-session handoff benchmarks.
- Faster commits by running independent model calls in parallel.
- Resolving merge conflicts from the web viewer.
- An optional free-tier deployment (Neon + Hugging Face Spaces + Gemini API key).

---

Made with ❤️ by Aayush
