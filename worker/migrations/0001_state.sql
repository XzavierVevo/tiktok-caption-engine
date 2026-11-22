CREATE TABLE IF NOT EXISTS documents (
  uid TEXT NOT NULL,
  collection TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  body_json TEXT,
  body_sha256 TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL,
  deleted_at INTEGER,
  storage_rel TEXT,
  PRIMARY KEY (uid, collection, doc_id)
);

CREATE INDEX IF NOT EXISTS documents_collection_active
  ON documents(uid, collection, deleted_at, updated_at);

CREATE TABLE IF NOT EXISTS migration_runs (
  run_id TEXT PRIMARY KEY,
  started_at INTEGER NOT NULL,
  completed_at INTEGER,
  source_snapshot TEXT NOT NULL,
  status TEXT NOT NULL,
  totals_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_items (
  run_id TEXT NOT NULL,
  uid TEXT NOT NULL,
  collection TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  source TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  target_hash TEXT,
  status TEXT NOT NULL,
  error TEXT,
  PRIMARY KEY (run_id, uid, collection, doc_id)
);
