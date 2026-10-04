# Decisions and deviations

## Verification status (phases 0-1)
- `pytest -q` (EMBED_BACKEND=fake): green. `ruff check .`: clean.
- `docker build` of the real Dockerfile FAILED in the agent sandbox: Hugging Face is
  unreachable (DNS), so `python -m hub.embeddings --download` fails, as the spec requires.
  Not verified: that `intfloat/multilingual-e5-small` ships `onnx/model.onnx` and loads via
  `TextEmbedding.add_custom_model`, and which fallback model wins. Run a real build and record
  the winner here.
- Smoke was run against an image built WITHOUT the download step, with
  `EMBED_BACKEND=fake` (`SMOKE_BUILD=0 scripts/smoke.sh`): passed, vector backend `sqlite-vec`.

## Deviations
- Legacy Postgres/sentence-transformers prototype (`indexer.py`, `query.py`, `web.py`,
  old compose file) moved to `legacy/` (excluded from ruff/Docker). Old `requirements.txt` and
  `.env.example` replaced per spec. Compose file is now `docker-compose.yml`.
- VectorStore methods take the caller's connection as first arg (`store.knn(conn, vec, k)`);
  connections are per request, so the store cannot own one.
- Compose sets `PORT=5000` inside the container so a host `PORT` in `.env` only changes the
  published port (otherwise gunicorn binds the wrong port).
- `POST /api/admin/reindex` returns 501 (phase 4). `needs_reindex` detection and 409s exist.
- `EMBED_BACKEND=openai` not implemented (phase 4); raises at startup.
- Search accepts `kinds` but only notes exist until phase 3.
- Snippet is a 400-char window around the first query term in the best chunk.
- `hub.db.connect` uses `isolation_level=None` (autocommit); `tx()` issues BEGIN IMMEDIATE.
- Versions pinned from the first green container build (python 3.11): see requirements.txt.
- `schema.sql` includes entity/pending tables now (idempotent DDL, unused until later phases).

## Phase 2 (AI tidy-up)
- No new dependencies: `openai` (already in requirements) is used via
  `client.chat.completions.create`; `BadRequestError` triggers the no-`response_format` retry.
- `notes.create_note` gained `raw_content`, `supersede` and `in_tx` so approve creates the note,
  supersedes the listed notes and marks the pending row approved in one transaction.
- Agent notes with AGENT_NOTES_REQUIRE_REVIEW=true become `ready` pending rows (202); `failed`
  and `ready` rows are approvable/editable/rejectable/regenerable.
- Job pool lives on `hub.queue` (created in `create_app`, not on import); startup requeues
  `processing` rows and purges rejected rows older than 30 days.
- Verified against an in-test stub server only; not against a real endpoint in this sandbox.
