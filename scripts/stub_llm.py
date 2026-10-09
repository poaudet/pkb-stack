"""Local canned OpenAI-compatible chat-completions server for Capture demos."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

G1 = {
    "summary": "Working with Matt; PMT launch on October 13",
    "ops": [
        {
            "id": "o1", "op": "create_entity", "type": "person", "ref": "matt",
            "data": {"name": "Matt", "relationship": "collaborator"},
        },
        {
            "id": "o2", "op": "create_entity", "type": "project", "ref": "pmt",
            "data": {"title": "PMT", "status": "active", "start_date": "2026-06-01",
                     "description": "Personal knowledge management tool (start day unknown)"},
        },
        {
            "id": "o3", "op": "create_entity", "type": "event",
            "data": {
                "date": "2026-10-13", "title": "PMT launch", "kind": "launch",
                "people": [{"$ref": "matt"}],
            },
        },
    ],
    "keep_note": False,
    "note": None,
    "questions": [],
}


def load_scenarios(directory: Path) -> list[dict[str, Any]]:
    scenarios = []
    for path in sorted(directory.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(scenario, dict) and isinstance(scenario.get("match"), str):
            scenarios.append(scenario)
    return scenarios


def make_server(host: str, port: int, fixtures_dir: Path,
                default_response: Any | None = None) -> ThreadingHTTPServer:
    scenarios = load_scenarios(fixtures_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not isinstance(request, dict) or "messages" not in request:
                    self.send_error(400)
                    return
                prompt = "\n".join(
                    message.get("content", "")
                    for message in request["messages"]
                    if isinstance(message, dict) and isinstance(message.get("content"), str)
                )
                response: Any = default_response if default_response is not None else G1
                if default_response is None:
                    for scenario in scenarios:
                        if scenario["match"] in prompt:
                            response = scenario.get("response", G1)
                            break
                content = response if isinstance(response, str) else json.dumps(response)
                body = {
                    "id": "capture-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.get("model", "capture-stub"),
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (KeyError, TypeError, ValueError):
                self.send_error(400)

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reply-file", type=Path)
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path(__file__).parents[1] / "tests" / "fixtures" / "capture",
    )
    args = parser.parse_args()
    response = (
        json.loads(args.reply_file.read_text(encoding="utf-8")) if args.reply_file else None
    )
    server = make_server(args.host, args.port, args.fixtures_dir, default_response=response)
    print(f"Stub LLM listening at http://{args.host}:{args.port}/v1")
    server.serve_forever()


if __name__ == "__main__":
    main()
