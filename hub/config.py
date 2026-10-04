"""Environment -> Settings."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    data_dir: str
    port: int
    api_token: str
    max_body_mb: int
    embed_backend: str
    embed_model: str
    embed_base_url: str
    embed_api_key: str
    model_cache_dir: str
    vector_backend: str
    llm_base_url: str
    llm_model: str
    agent_notes_require_review: bool
    mirror_markdown: bool
    backup_keep: int
    log_level: str

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "knowledge.db")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=_str("DATA_DIR", "/data"),
            port=_int("PORT", 5000),
            api_token=_str("API_TOKEN"),
            max_body_mb=_int("MAX_BODY_MB", 2),
            embed_backend=_str("EMBED_BACKEND", "local"),
            embed_model=_str("EMBED_MODEL", "intfloat/multilingual-e5-small"),
            embed_base_url=_str("EMBED_BASE_URL"),
            embed_api_key=_str("EMBED_API_KEY"),
            model_cache_dir=_str("MODEL_CACHE_DIR", "/opt/models"),
            vector_backend=_str("VECTOR_BACKEND", "auto"),
            llm_base_url=_str("LLM_BASE_URL"),
            llm_model=_str("LLM_MODEL"),
            agent_notes_require_review=_bool("AGENT_NOTES_REQUIRE_REVIEW", True),
            mirror_markdown=_bool("MIRROR_MARKDOWN", True),
            backup_keep=_int("BACKUP_KEEP", 14),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
        )
