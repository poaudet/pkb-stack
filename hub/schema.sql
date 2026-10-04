CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  raw_content TEXT,
  content_hash TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
  happened_at TEXT,
  source TEXT NOT NULL DEFAULT 'ui',
  status TEXT NOT NULL DEFAULT 'current'
    CHECK (status IN ('current','superseded','archived')),
  superseded_by INTEGER REFERENCES notes(id) ON DELETE SET NULL,
  status_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_hash ON notes(content_hash);

CREATE TABLE IF NOT EXISTS pending_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  raw_content TEXT NOT NULL,
  title TEXT, content TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  happened_at TEXT,
  warnings TEXT NOT NULL DEFAULT '{}',
  supersedes TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'ui',
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
    ('queued','processing','ready','failed','approved','rejected')),
  error TEXT, llm_model TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  decided_at TEXT);

CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('note','entity')),
  owner_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  UNIQUE(owner_kind, owner_id, idx));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, text, content='chunks', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid,title,text)
    VALUES(new.id,new.title,new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,text)
    VALUES('delete',old.id,old.title,old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts,rowid,title,text)
    VALUES('delete',old.id,old.title,old.text);
  INSERT INTO chunks_fts(rowid,title,text)
    VALUES(new.id,new.title,new.text);
END;

CREATE TABLE IF NOT EXISTS entity_types(
  name TEXT PRIMARY KEY CHECK (name GLOB '[a-z]*'
    AND name NOT GLOB '*[^a-z0-9_]*'),
  description TEXT NOT NULL DEFAULT '',
  json_schema TEXT NOT NULL CHECK (json_valid(json_schema)),
  text_template TEXT,
  unique_key TEXT,
  embed INTEGER NOT NULL DEFAULT 1,
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));

CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL REFERENCES entity_types(name),
  data TEXT NOT NULL CHECK (json_valid(data)),
  source TEXT NOT NULL DEFAULT 'api',
  status TEXT NOT NULL DEFAULT 'current'
    CHECK (status IN ('current','superseded','archived')),
  superseded_by INTEGER REFERENCES entities(id) ON DELETE SET NULL,
  status_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type, status);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  actor TEXT NOT NULL, action TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_uid TEXT, detail TEXT);
