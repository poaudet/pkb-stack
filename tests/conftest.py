import pytest

from hub import create_app
from hub.config import Settings


def make_settings(tmp_path, **kw):
    base = dict(  # noqa: C408
        data_dir=str(tmp_path), port=5000, api_token="", max_body_mb=2, embed_backend="fake",
        embed_model="fake", embed_base_url="", embed_api_key="", model_cache_dir="",
        vector_backend="auto", llm_base_url="", llm_model="", agent_notes_require_review=True,
        mirror_markdown=True, backup_keep=2, log_level="WARNING",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def mk(tmp_path):
    def _mk(**kw):
        return create_app(make_settings(tmp_path, **kw))
    return _mk


@pytest.fixture
def app(mk):
    return mk()


@pytest.fixture
def client(app):
    return app.test_client()
