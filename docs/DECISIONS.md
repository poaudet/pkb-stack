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
- `needs_reindex` detection and 409s exist (reindex itself: phase 4).
- Search accepts `kinds`; entity hits arrive in phase 3.
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

## Phase 3 (entities + agent tools)
- No new dependencies: `jsonschema` (already pinned) validates types and data. `date`/`date-time`
  formats are checked by a custom `format` keyword (`validators.extend`), not format packages.
- Type PATCH "additive" = all existing properties kept unchanged, no new `required` entries.
  Changing `text_template`/`embed`/schema does not re-embed existing entities (reindex is phase 4).
- Entity types whose `embed` is 0 get no chunk, so they are not searchable, only queryable.
- Search `types` filter returns entity hits of those types only; `kinds` filters note/entity.
  Entity hit date for since/until = `data.date` if present, else `created_at`; tags = `data.tags`.
- `POST /api/tools/{name}` defaults the actor to `agent:api` when `X-Actor` is absent so agent
  notes always follow AGENT_NOTES_REQUIRE_REVIEW. Destructive tools answer 404 unless
  TOOLS_ALLOW_DESTRUCTIVE=true.
- Entity audit `detail` holds only the type name / new uid / forget reason, never data.
- Restoring an archived entity re-checks `unique_key` (409 if a current one now owns it).
- UI shows entity details as read-only JSON (archive/restore/forget); edits go through the API/tools.
- SKILL.md front matter (`name`, `description`) was not verified against agentskills.io: no network.

## Phase 4 (extras)
- New dependency `mcp==2.3.0` (official MCP Python SDK), in `requirements-mcp.txt` only and
  installed by `Dockerfile.mcp`; the hub image does not include it. `httpx`/`uvicorn` come with
  it. mcp 2.x renamed FastMCP to MCPServer; the bridge uses the low-level `Server`
  (`on_list_tools`/`on_call_tool`) so each tool's JSON schema from `/api/tools` is passed through.
  Calls are forwarded with `X-Actor: agent:mcp` (`MCP_ACTOR`). Transport via `MCP_TRANSPORT`.
- `EMBED_BACKEND=openai` uses the existing `openai` package (`client.embeddings.create`); the
  dimension is probed at startup with one request. Compose `mcp` profile publishes port 8000.
- Reindex: `POST /api/admin/reindex` (202) starts one in-process thread; `GET` returns progress.
  If the model changed it drops/recreates the vector table at the new dimension, then rewrites
  chunks per note/entity (embedding outside the write transaction). Writes stay 409 until done.
- Inbox import moves only created/queued files to `inbox/_processed/<ts>/`; duplicates stay put.
- Export zip is rendered from the DB (works even with MIRROR_MARKDOWN=false).
- Optional OpenAPI doc not implemented.
- rclone backup profile already existed; see README.
- Verification: see PR description (pytest, ruff, docker build).
