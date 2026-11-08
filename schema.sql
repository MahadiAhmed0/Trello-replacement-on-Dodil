-- Tasklane's tables. The remix applies this file once, to the bucket it creates.
-- Natural keys only: DataK3 has no sequences, and a write that omits its key fails silently.

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner_email TEXT NOT NULL,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_members (
  id TEXT PRIMARY KEY,              -- workspace_id || ':' || email
  workspace_id TEXT NOT NULL,
  email TEXT NOT NULL,
  role TEXT NOT NULL,               -- member | viewer (the owner lives on workspaces)
  added_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS boards (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  title TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS board_members (
  id TEXT PRIMARY KEY,              -- board_id || ':' || email
  board_id TEXT NOT NULL,
  email TEXT NOT NULL,
  added_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS board_lists (
  id TEXT PRIMARY KEY,
  board_id TEXT NOT NULL,
  name TEXT NOT NULL,
  sort_key DOUBLE NOT NULL,
  is_done BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
  card_no BIGINT PRIMARY KEY,       -- "card 12": claimed by a plain INSERT, never an upsert
  board_id TEXT NOT NULL,
  list_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  assignee TEXT,
  due_date DATE,
  labels TEXT,                      -- JSON array of strings
  sort_key DOUBLE NOT NULL,
  version BIGINT NOT NULL,          -- optimistic concurrency: an edit names the version it read
  created_by TEXT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS card_links (
  id TEXT PRIMARY KEY,              -- card_no || '<' || blocked_by
  board_id TEXT NOT NULL,
  card_no BIGINT NOT NULL,          -- this card ...
  blocked_by BIGINT NOT NULL,       -- ... waits for this one
  created_by TEXT,
  created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS activity (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  board_id TEXT,
  card_no BIGINT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  changes TEXT,                     -- JSON: [{"field", "before", "after"}]
  event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS card_vectors (
  card_no BIGINT PRIMARY KEY,
  board_id TEXT NOT NULL,
  embedding VECTOR(2048)            -- jina-embeddings-v4 via Dodil Models
);
