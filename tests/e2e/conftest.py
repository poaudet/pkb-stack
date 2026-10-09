from __future__ import annotations

import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from hub import create_app
from scripts.stub_llm import make_server as make_stub_server
from tests.conftest import make_settings

TOKEN = "e2e-test-token"


@pytest.fixture
def ui_server(tmp_path):
    servers = []

    def start(**settings_overrides):
        fixtures = Path(__file__).parents[1] / "fixtures" / "capture"
        stub = make_stub_server("127.0.0.1", 0, fixtures)
        stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
        stub_thread.start()
        settings = make_settings(
            tmp_path / f"app-{len(servers)}",
            api_token=TOKEN,
            llm_model="capture-test",
            capture_llm_base_url=f"http://127.0.0.1:{stub.server_port}/v1",
            **settings_overrides,
        )
        app = create_app(settings)
        server = make_server("127.0.0.1", 0, app)
        app_thread = threading.Thread(target=server.serve_forever, daemon=True)
        app_thread.start()
        servers.append((server, app_thread, stub, stub_thread, app))
        return f"http://127.0.0.1:{server.server_port}"

    yield start

    for server, app_thread, stub, stub_thread, app in reversed(servers):
        server.shutdown()
        app_thread.join(timeout=2)
        stub.shutdown()
        stub.server_close()
        stub_thread.join(timeout=2)
        app.extensions["hub"].capture_queue.pool.shutdown(wait=True)
        app.extensions["hub"].queue.pool.shutdown(wait=True)
