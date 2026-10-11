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
        self.reindexer = None  # reindex.Reindexer, attached by create_app
        for sub in ("", "notes", "inbox", "backups"):
            os.makedirs(os.path.join(settings.data_dir, sub), exist_ok=True)
        conn = db.connect(settings.db_path)
        try:
            db.migrate(conn)
            entities.seed_types(conn)
            self.embedder = embedder or Embedder(
                settings.embed_backend, settings.embed_model, settings.model_cache_dir,
                settings.embed_base_url, settings.embed_api_key,
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

    def llm_settings(self, purpose: str) -> Settings:
        """Settings with the user-selected LLM endpoint applied for 'tidy' or 'capture'."""
        from . import llm_endpoints

        if not self.settings.llm_endpoints:
            return self.settings
        conn = self.conn()
        try:
            return llm_endpoints.effective(conn, self.settings, purpose)
        finally:
            conn.close()

    def conn(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path, load_vec=self.store.backend == "sqlite-vec")
