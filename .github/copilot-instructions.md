Follow docs/BUILD_SPEC.md. Implement phases in order, one PR each.
Python 3.11, Flask, stdlib sqlite3 (no ORM), type hints, ruff-clean.
Parameterized SQL only; identifiers come from code or whitelists.
Never invent library APIs: read docs/README or help(), then run it.
Run `pytest -q` and `docker compose build` before finishing; say so
if Docker is unavailable. No new dependencies without a note in
docs/DECISIONS.md. Never log note/entity content above DEBUG.
