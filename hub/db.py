"""SQLite connection, migrations, transactions."""
from __future__ import annotations

import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def fold(value: str | None) -> str | None:
    """Case-fold text and remove combining marks for accent-insensitive matching."""
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def connect(path: str, load_vec: bool = True) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < (3, 35):
        raise RuntimeError(f"SQLite >= 3.35 required, got {sqlite3.sqlite_version}")
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if load_vec:
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as e:  # noqa: BLE001 - numpy fallback handles absence
            logging.getLogger(__name__).debug("sqlite-vec not loaded: %s", e)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Short write transaction (BEGIN IMMEDIATE ... COMMIT/ROLLBACK)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _m1(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _m2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS captures(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uid TEXT NOT NULL UNIQUE,
          raw_text TEXT,
          text_hash TEXT NOT NULL,
          hint TEXT,
          as_of TEXT,
          source TEXT NOT NULL DEFAULT 'ui',
          state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN (
            'queued','processing','ready','failed','applied','rejected','undone')),
          plan TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(plan)),
          questions TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(questions)),
          answers TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(answers)),
          warnings TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),
          error TEXT,
          llm_model TEXT,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
          decided_at TEXT          );
          CREATE INDEX IF NOT EXISTS idx_captures_state ON captures(state, created_at DESC);

          CREATE TABLE IF NOT EXISTS capture_ops(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
          op_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          entity_type TEXT,
          target_uid TEXT,
          result_uid TEXT,
          before TEXT,
          after_hash TEXT,
          status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied','undone')));

        CREATE TABLE IF NOT EXISTS capture_usage(
          day TEXT PRIMARY KEY,
          calls INTEGER NOT NULL DEFAULT 0);
        """
    )


def _m3(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_captures_hash_state "
        "ON captures(text_hash, state, created_at DESC)"
    )


def _m4(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entity_types)")}
    if "schema_version" not in columns:
        conn.execute(
            "ALTER TABLE entity_types ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_type_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          type TEXT NOT NULL REFERENCES entity_types(name) ON DELETE CASCADE,
          version INTEGER NOT NULL,
          json_schema TEXT NOT NULL CHECK (json_valid(json_schema)),
          change TEXT NOT NULL CHECK (json_valid(change)),
          actor TEXT NOT NULL,
          capture_uid TEXT,
          at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
          UNIQUE(type, version)
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_type_history(type,version,json_schema,change,actor) "
        "SELECT name,1,json_schema,'{}','migration' FROM entity_types"
    )


def _m5(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entity_types)")}
    for name in ("capture_visible", "agent_visible", "auditable"):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE entity_types ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1"
            )


def _m6(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(entity_types)")}
    if "allow_extension" not in columns:
        conn.execute(
            "ALTER TABLE entity_types ADD COLUMN allow_extension INTEGER NOT NULL DEFAULT 1"
        )


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_m1, _m2, _m3, _m4, _m5, _m6]


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 0
    for n, fn in enumerate(MIGRATIONS, start=1):
        if n > current:
            fn(conn)
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(n),),
            )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
