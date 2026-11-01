"""Initial schema.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
  id           uuid PRIMARY KEY,
  idp_subject  text UNIQUE NOT NULL,
  email        text,
  display_name text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_users_email ON users (lower(email));

CREATE TABLE repos (
  id          uuid PRIMARY KEY,
  owner_id    uuid NOT NULL REFERENCES users(id),
  name        text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
  description text,
  settings    jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_id, name)
);

CREATE TABLE repo_members (
  repo_id uuid REFERENCES repos(id) ON DELETE CASCADE,
  user_id uuid REFERENCES users(id) ON DELETE CASCADE,
  role    text NOT NULL CONSTRAINT role_valid CHECK (role IN ('owner','editor','viewer')),
  PRIMARY KEY (repo_id, user_id)
);
CREATE INDEX ix_repo_members_user ON repo_members (user_id);

CREATE TABLE commits (
  id            uuid PRIMARY KEY,
  repo_id       uuid NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  author_id     uuid NOT NULL REFERENCES users(id),
  message       text NOT NULL,
  kind          text NOT NULL DEFAULT 'normal'
                CHECK (kind IN ('root','normal','merge','revert')),
  source_client text,
  source_model  text,
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_commits_repo ON commits (repo_id, created_at);

CREATE TABLE commit_parents (
  commit_id uuid REFERENCES commits(id) ON DELETE CASCADE,
  parent_id uuid REFERENCES commits(id) ON DELETE CASCADE,
  ord       smallint NOT NULL,
  PRIMARY KEY (commit_id, parent_id)
);
CREATE INDEX ix_commit_parents_parent ON commit_parents (parent_id);

CREATE TABLE branches (
  repo_id        uuid REFERENCES repos(id) ON DELETE CASCADE,
  name           text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'),
  head_commit_id uuid NOT NULL REFERENCES commits(id),
  fork_commit_id uuid REFERENCES commits(id),
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (repo_id, name)
);

CREATE TABLE user_heads (
  user_id   uuid REFERENCES users(id) ON DELETE CASCADE,
  repo_id   uuid REFERENCES repos(id) ON DELETE CASCADE,
  branch    text,
  commit_id uuid REFERENCES commits(id) ON DELETE SET NULL,
  PRIMARY KEY (user_id, repo_id)
);

CREATE TABLE state_items (
  id           uuid PRIMARY KEY,
  repo_id      uuid NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  item_key     uuid NOT NULL,
  type         text NOT NULL CHECK (type IN
                 ('goal','constraint','decision','open_question','fact','task')),
  subject      text NOT NULL,
  subject_norm text NOT NULL,
  content      text NOT NULL,
  compressed   text,
  attributes   jsonb NOT NULL DEFAULT '{}',
  embedding    vector(768),
  importance   real NOT NULL DEFAULT 0.5,
  llm_score    real,
  pinned       boolean NOT NULL DEFAULT false,
  use_count    integer NOT NULL DEFAULT 0,
  created_in   uuid NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  provenance   jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_state_items_key ON state_items (repo_id, item_key);
CREATE INDEX ix_state_items_subject ON state_items (repo_id, subject_norm);
CREATE INDEX ix_state_items_embedding ON state_items USING hnsw (embedding vector_cosine_ops);

CREATE TABLE commit_items (
  commit_id     uuid REFERENCES commits(id) ON DELETE CASCADE,
  item_id       uuid REFERENCES state_items(id) ON DELETE CASCADE,
  status        text NOT NULL CHECK (status IN ('active','superseded','resolved','retracted')),
  superseded_by uuid REFERENCES state_items(id) ON DELETE CASCADE,
  PRIMARY KEY (commit_id, item_id)
);
CREATE INDEX ix_commit_items_item ON commit_items (item_id);

CREATE TABLE proposals (
  id               uuid PRIMARY KEY,
  repo_id          uuid NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  branch           text NOT NULL,
  base_commit_id   uuid NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
  author_id        uuid NOT NULL REFERENCES users(id),
  message          text NOT NULL,
  source_client    text,
  source_model     text,
  entries          jsonb NOT NULL,
  audit            jsonb NOT NULL,
  status           text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','confirmed','discarded','expired')),
  result_commit_id uuid REFERENCES commits(id),
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL
);

CREATE TABLE merges (
  id            uuid PRIMARY KEY,
  repo_id       uuid NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  ours_branch   text NOT NULL,
  theirs_ref    text NOT NULL,
  ours_commit   uuid NOT NULL REFERENCES commits(id),
  theirs_commit uuid NOT NULL REFERENCES commits(id),
  base_commit   uuid NOT NULL REFERENCES commits(id),
  state         text NOT NULL CHECK (state IN ('pending','completed','aborted')),
  plan          jsonb NOT NULL DEFAULT '{}',
  result_commit uuid REFERENCES commits(id),
  created_by    uuid NOT NULL REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
-- At most one pending merge into a branch at a time.
CREATE UNIQUE INDEX ux_merges_pending ON merges (repo_id, ours_branch)
  WHERE state = 'pending';

CREATE TABLE merge_conflicts (
  id          uuid PRIMARY KEY,
  merge_id    uuid NOT NULL REFERENCES merges(id) ON DELETE CASCADE,
  ord         integer NOT NULL DEFAULT 0,
  kind        text NOT NULL CHECK (kind IN ('contradiction','temporal_scope','both_modified')),
  base_item   uuid REFERENCES state_items(id) ON DELETE CASCADE,
  ours_item   uuid REFERENCES state_items(id) ON DELETE CASCADE,
  theirs_item uuid REFERENCES state_items(id) ON DELETE CASCADE,
  details     jsonb NOT NULL DEFAULT '{}',
  resolution  text CHECK (resolution IN ('ours','theirs','both','edited')),
  edited      jsonb,
  resolved_by uuid REFERENCES users(id)
);

CREATE TABLE pipeline_audit (
  id         bigserial PRIMARY KEY,
  commit_id  uuid REFERENCES commits(id) ON DELETE CASCADE,
  stage      text NOT NULL,
  decision   text NOT NULL,
  details    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pipeline_audit_commit ON pipeline_audit (commit_id);

CREATE TABLE uploads (
  id           uuid PRIMARY KEY,
  repo_id      uuid NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  created_by   uuid NOT NULL REFERENCES users(id),
  branch       text NOT NULL,
  source       text NOT NULL CHECK (source IN ('claude_export','chatgpt_export')),
  filename     text,
  size_bytes   bigint NOT NULL,
  gcs_uri      text,
  content      bytea,
  conversation text,
  status       text NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','running','done','failed')),
  error        text,
  proposal_id  uuid REFERENCES proposals(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id         uuid PRIMARY KEY,
  kind       text NOT NULL,
  payload    jsonb NOT NULL,
  status     text NOT NULL DEFAULT 'queued',
  attempts   integer NOT NULL DEFAULT 0,
  error      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_jobs_queued ON jobs (created_at) WHERE status = 'queued';

CREATE TABLE oauth_clients (
  client_id  text PRIMARY KEY,
  info       jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE oauth_pending_auth (
  id         text PRIMARY KEY,
  client_id  text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
  params     jsonb NOT NULL,
  expires_at timestamptz NOT NULL
);

CREATE TABLE oauth_codes (
  code_hash  text PRIMARY KEY,
  data       jsonb NOT NULL,
  expires_at timestamptz NOT NULL
);

CREATE TABLE oauth_tokens (
  token_hash text PRIMARY KEY,
  kind       text NOT NULL CHECK (kind IN ('access','refresh')),
  family_id  uuid NOT NULL,
  client_id  text NOT NULL,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  scopes     jsonb NOT NULL,
  resource   text,
  expires_at timestamptz NOT NULL,
  revoked    boolean NOT NULL DEFAULT false
);
CREATE INDEX ix_oauth_tokens_family ON oauth_tokens (family_id);

CREATE TABLE rate_limits (
  key          text NOT NULL,
  window_start timestamptz NOT NULL,
  count        integer NOT NULL DEFAULT 0,
  PRIMARY KEY (key, window_start)
);
"""

TABLES = [
    "rate_limits",
    "oauth_tokens",
    "oauth_codes",
    "oauth_pending_auth",
    "oauth_clients",
    "jobs",
    "uploads",
    "pipeline_audit",
    "merge_conflicts",
    "merges",
    "proposals",
    "commit_items",
    "state_items",
    "user_heads",
    "branches",
    "commit_parents",
    "commits",
    "repo_members",
    "repos",
    "users",
]


def upgrade() -> None:
    # asyncpg runs one statement per call.
    for stmt in SCHEMA.split(";\n"):
        if stmt.strip():
            op.execute(stmt)


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
