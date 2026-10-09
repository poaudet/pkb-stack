"""Local canned OpenAI-compatible chat-completions server for Capture demos."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

G1 = {
    "summary": "Working with Matt; PMT launch on October 13",
    "ops": [
        {
            "id": "o1", "op": "create_entity", "type": "person", "ref": "matt",
            "data": {"name": "Matt", "relationship": "collaborator"},
        },
        {
            "id": "o2", "op": "create_entity", "type": "event",
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reply-file", type=Path)
    args = parser.parse_args()
    reply = json.dumps(
        json.loads(args.reply_file.read_text(encoding="utf-8")) if args.reply_file else G1
    )

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
                body = {
                    "id": "capture-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request.get("model", "capture-stub"),
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": reply},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (KeyError, ValueError):
                self.send_error(400)

    print(f"Stub LLM listening at http://{args.host}:{args.port}/v1")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
