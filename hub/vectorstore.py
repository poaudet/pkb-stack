"""Vector stores: sqlite-vec (vec0) with numpy fallback.

All methods take the caller's connection (connections are per request/thread).
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading

import numpy as np

log = logging.getLogger(__name__)


class NumpyStore:
    backend = "numpy"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix: np.ndarray | None = None
        self._ids: np.ndarray | None = None
        self.dim = 0

    def ensure(self, conn: sqlite3.Connection, dim: int) -> None:
        self.dim = int(dim)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunk_vectors("
            "chunk_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
        )

    def reset(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS chunk_vectors")
        self._invalidate()

    def upsert(self, conn: sqlite3.Connection, chunk_id: int, vec: np.ndarray) -> None:
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET embedding=excluded.embedding",
            (chunk_id, np.asarray(vec, dtype=np.float32).tobytes()),
        )
        self._invalidate()

    def delete(self, conn: sqlite3.Connection, ids: list[int]) -> None:
        for i in ids:
            conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (i,))
        self._invalidate()

    def _invalidate(self) -> None:
        with self._lock:
            self._matrix = None

    def knn(self, conn: sqlite3.Connection, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        with self._lock:
            if self._matrix is None:
                rows = conn.execute("SELECT chunk_id, embedding FROM chunk_vectors").fetchall()
                self._ids = np.array([r[0] for r in rows], dtype=np.int64)
                self._matrix = (
                    np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                    if rows
                    else np.zeros((0, self.dim), dtype=np.float32)
                )
            matrix, ids = self._matrix, self._ids
        if matrix is None or len(ids) == 0:
            return []
        sims = matrix @ np.asarray(vec, dtype=np.float32)
        k = min(k, len(ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(ids[i]), math.sqrt(max(0.0, 2.0 - 2.0 * float(sims[i])))) for i in top]


class SqliteVecStore:
    backend = "sqlite-vec"

    def __init__(self) -> None:
        self.dim = 0

    def ensure(self, conn: sqlite3.Connection, dim: int) -> None:
        self.dim = int(dim)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{self.dim}])"
        )

    def reset(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS vec_chunks")

    def upsert(self, conn: sqlite3.Connection, chunk_id: int, vec: np.ndarray) -> None:
        import sqlite_vec

        conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (chunk_id,))
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
            (chunk_id, sqlite_vec.serialize_float32(np.asarray(vec, dtype=np.float32).tolist())),
        )

    def delete(self, conn: sqlite3.Connection, ids: list[int]) -> None:
        for i in ids:
            conn.execute("DELETE FROM vec_chunks WHERE rowid=?", (i,))

    def knn(self, conn: sqlite3.Connection, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        import sqlite_vec

        rows = conn.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (sqlite_vec.serialize_float32(np.asarray(vec, dtype=np.float32).tolist()), int(k)),
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]


def _self_test(conn: sqlite3.Connection) -> None:
    import sqlite_vec

    conn.execute("CREATE VIRTUAL TABLE temp.vec_selftest USING vec0(embedding float[4])")
    try:
        conn.execute(
            "INSERT INTO temp.vec_selftest(rowid, embedding) VALUES (1, ?)",
            (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
        )
        rows = conn.execute(
            "SELECT rowid, distance FROM temp.vec_selftest WHERE embedding MATCH ? AND k = 1",
            (sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0]),),
        ).fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise RuntimeError("sqlite-vec self-test returned wrong result")
    finally:
        conn.execute("DROP TABLE IF EXISTS temp.vec_selftest")


def select_store(conn: sqlite3.Connection, preference: str) -> SqliteVecStore | NumpyStore:
    if preference == "numpy":
        return NumpyStore()
    try:
        _self_test(conn)
        return SqliteVecStore()
    except Exception as e:
        if preference == "sqlite-vec":
            raise
        log.warning("sqlite-vec unavailable (%s); using numpy vector store", e)
        return NumpyStore()
