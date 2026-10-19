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
    llm_api_key: str = "not-needed"
    llm_timeout: int = 300
    llm_concurrency: int = 1
    llm_max_input_chars: int = 12000
    llm_endpoints: str = ""
    tools_allow_destructive: bool = False
    tools_allow_extend: bool = True
    capture_enabled: bool = True
    capture_llm_base_url: str = ""
    capture_llm_api_key: str = ""
    capture_llm_model: str = ""
    capture_llm_extra_headers: str = ""
    capture_llm_timeout: int = 120
    capture_max_chars: int = 8000
    capture_max_ops: int = 20
    capture_candidates: int = 25
    capture_exclude_types: str = "place_visit,task,grocery_list,grocery_item,plainlist_settings"
    capture_block_secrets: bool = True
    capture_daily_call_limit: int = 60
    capture_keep_raw_days: int = 30
    hub_timezone: str = "UTC"
    owner_name: str = ""
    capture_allow_schema_changes: bool = True
    capture_max_new_fields: int = 5
    capture_gap_fill: bool = True
    capture_gap_max: int = 8
    capture_gap_default_include: bool = True
    capture_catalog_max_chars: int = 5000

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
            llm_api_key=_str("LLM_API_KEY", "not-needed"),
            llm_timeout=_int("LLM_TIMEOUT", 300),
            llm_concurrency=max(1, _int("LLM_CONCURRENCY", 1)),
            llm_max_input_chars=_int("LLM_MAX_INPUT_CHARS", 12000),
            llm_endpoints=_str("LLM_ENDPOINTS"),
            tools_allow_destructive=_bool("TOOLS_ALLOW_DESTRUCTIVE", False),
            tools_allow_extend=_bool("TOOLS_ALLOW_EXTEND", True),
            capture_enabled=_bool("CAPTURE_ENABLED", True),
            capture_llm_base_url=_str("CAPTURE_LLM_BASE_URL"),
            capture_llm_api_key=_str("CAPTURE_LLM_API_KEY"),
            capture_llm_model=_str("CAPTURE_LLM_MODEL"),
            capture_llm_extra_headers=_str("CAPTURE_LLM_EXTRA_HEADERS"),
            capture_llm_timeout=_int("CAPTURE_LLM_TIMEOUT", 120),
            capture_max_chars=_int("CAPTURE_MAX_CHARS", 8000),
            capture_max_ops=_int("CAPTURE_MAX_OPS", 20),
            capture_candidates=_int("CAPTURE_CANDIDATES", 25),
            capture_exclude_types=_str(
                "CAPTURE_EXCLUDE_TYPES",
                "place_visit,task,grocery_list,grocery_item,plainlist_settings",
            ),
            capture_block_secrets=_bool("CAPTURE_BLOCK_SECRETS", True),
            capture_daily_call_limit=_int("CAPTURE_DAILY_CALL_LIMIT", 60),
            capture_keep_raw_days=_int("CAPTURE_KEEP_RAW_DAYS", 30),
            hub_timezone=_str("HUB_TIMEZONE", "UTC"),
            owner_name=_str("OWNER_NAME"),
            capture_allow_schema_changes=_bool("CAPTURE_ALLOW_SCHEMA_CHANGES", True),
            capture_max_new_fields=_int("CAPTURE_MAX_NEW_FIELDS", 5),
            capture_gap_fill=_bool("CAPTURE_GAP_FILL", True),
            capture_gap_max=_int("CAPTURE_GAP_MAX", 8),
            capture_gap_default_include=_bool("CAPTURE_GAP_DEFAULT_INCLUDE", True),
            capture_catalog_max_chars=_int("CAPTURE_CATALOG_MAX_CHARS", 5000),
        )
