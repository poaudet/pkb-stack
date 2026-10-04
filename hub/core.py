"""Shared application state (settings, embedder, vector store, DB access)."""
from __future__ import annotations

import logging
import os
import sqlite3

from . import db, entities
from .config import Settings
from .embeddings import Embedder
from .vectorstore import NumpyStore, SqliteVecStore, select_store

log = logging.getLogger(__name__)


class Hub:
    def __init__(self, settings: Settings, embedder: Embedder | None = None) -> None:
        self.settings = settings
        self.queue = None  # pending.Queue, attached by create_app
        for sub in ("", "notes", "inbox", "backups"):
            os.makedirs(os.path.join(settings.data_dir, sub), exist_ok=True)
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
            entities.seed_types(conn)
            self.embedder = embedder or Embedder(
                settings.embed_backend, settings.embed_model, settings.model_cache_dir
            )
            self.store: SqliteVecStore | NumpyStore = select_store(conn, settings.vector_backend)
            stored = db.get_meta(conn, "embed_model")
            if stored is None:
                db.set_meta(conn, "embed_model", self.embedder.model)
                db.set_meta(conn, "embed_dim", str(self.embedder.dim))
                stored = self.embedder.model
            self.needs_reindex = stored != self.embedder.model
            if not self.needs_reindex:
                self.store.ensure(conn, int(db.get_meta(conn, "embed_dim") or self.embedder.dim))
        finally:
            conn.close()
        if not settings.api_token:
            log.warning("API_TOKEN is empty: the API is unauthenticated. Set it before exposing.")

    def conn(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path, load_vec=self.store.backend == "sqlite-vec")
