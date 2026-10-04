"""Seed demo people, events and messages through the HTTP API.

Usage: python scripts/seed_demo.py [base_url]   (API_TOKEN env var is sent if set)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable

PEOPLE = [
    {"name": "Alice", "birthday": "1990-04-12", "relationship": "friend",
     "habits": ["runs every morning", "drinks green tea"], "interests": ["climbing"]},
    {"name": "Bob", "birthday": "1985-11-02", "relationship": "friend",
     "habits": ["plays chess on Sundays"], "interests": ["chess", "cooking"]},
    {"name": "Vet", "relationship": "service", "contact": {"phone": "555-0100"}},
]
EVENTS = [
    {"date": "2025-06-14", "title": "Hike", "kind": "outing", "location": "Mont Royal",
     "people": ["Alice"]},
    {"date": "2025-09-03", "title": "Picnic", "kind": "outing", "location": "Mont Royal"},
    {"date": "2025-07-20", "title": "Concert", "kind": "outing", "location": "Place des Arts"},
]
MESSAGES = [
    {"contact": "Vet", "at": "2025-08-01T09:30:00", "direction": "out", "channel": "sms",
     "text": "Can we book a check-up for Milo?"},
    {"contact": "Vet", "at": "2025-08-01T10:05:00", "direction": "in", "channel": "sms",
     "text": "Yes, Thursday at 3pm works."},
    {"contact": "Alice", "at": "2025-08-02T18:00:00", "direction": "in", "channel": "chat",
     "text": "Climbing this weekend?"},
]

Call = Callable[[str, str, dict | None], dict]


def seed(call: Call) -> None:
    for kind, rows in (("person", PEOPLE), ("event", EVENTS), ("message", MESSAGES)):
        for data in rows:
            call("POST", "/api/entities", {"type": kind, "data": data})


def _http(base: str) -> Call:
    def call(method: str, path: str, body: dict | None) -> dict:
        headers = {"Content-Type": "application/json", "X-Actor": "agent:seed"}
        if os.environ.get("API_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["API_TOKEN"]
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    return call


if __name__ == "__main__":
    seed(_http(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"))
    print("seeded")
