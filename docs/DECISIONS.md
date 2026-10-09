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

## Capture preconditions and C0-C2
- §0 audit: the OpenAI-compatible `hub/llm.py` with JSON-mode fallback and tolerant parser,
  entity services/type validation/unique keys and `hub/tools.py`, and the `ThreadPoolExecutor`
  queue with startup recovery were already present. The repository identifies phases 0-4 as
  complete; the existing and new suite passes together (63 passed, 1 skipped), and `ruff check .`
  is clean.
- Gap found before implementation: SQLite `fold()` and all `hub/capture/` modules/tables were
  absent. Added the deterministic NFKD + casefold + combining-mark removal function to every
  SQLite connection, then built Capture on a new schema migration.
- `pytest` and `ruff` were not installed in the sandbox initially; installed the already-pinned
  `requirements.txt` and `requirements-dev.txt` packages locally to run validation. No project
  dependency was added. Docker CLI is present; build/run result is recorded after verification.
- `HUB_TIMEZONE=UTC` is now an optional setting, as required by the Capture addendum. Capture
  requests use it to choose `as_of`; raw text is also purged at startup and by a daily timer.
- The Capture HTTP stub is a stdlib-only canned-plan server in `scripts/stub_llm.py`; automated
  pipeline tests use an in-process OpenAI-compatible stub. No live LLM endpoint was contacted.
- Entity create/supersede hooks accept pre-generated UIDs, precomputed embeddings, and an explicit
  existing-transaction mode; update accepts precomputed embeddings. Capture suppresses per-entity
  audit rows in favor of one ID-only `capture_apply` row.
- Verification: `pytest -q` (63 passed, 1 skipped), `ruff check .`, and
  `docker compose config --quiet` pass. `docker compose build` reaches the model-download step but
  fails because the sandbox cannot resolve the Hugging Face host. A Python 3.11.17 Docker runtime
  smoke test with `EMBED_BACKEND=fake` started the app and returned `/health` status `ok`; this
  does not verify the production image's baked model.
- Scope stops after C2: no Capture page (C3) or `capture_text`/tool profile integration (C4).

## Self/gap/schema precondition audit (D0-D2)
- Capture tables and modules are present; atomic apply and conditional undo are exercised by
  `tests/test_capture.py`. Existing reference resolution is limited to plan aliases/candidates;
  an unresolved `$ref` fails during apply as a generic `capture_apply` conflict, with no
  dependency-closure error. This is a gap addressed by D1.
- Entity chunk text is rendered by `entities.render_text`; entity data is checked by
  `entities.validate_data`.
- `PATCH /api/entity-types/{name}` currently uses `_is_additive`: existing properties must remain
  identical and new required fields are rejected, but removing an existing `required` entry is
  allowed. D2's extension API will be stricter and separate; existing PATCH behavior is unchanged.
- `fold()` is registered by `db.connect` on every SQLite connection.
- `capture_ops.kind` has no CHECK constraint, so `extend_entity_type` needs no table rebuild.
- D0-D2 were not present: no owner pointer/guards or owner API, no DB-backed reference matching,
  missing-reference gaps or operation dependencies, and no schema-version/history/extension flow.
- `pytest` was unavailable in the initial sandbox PATH; validation dependencies are listed in
  `requirements-dev.txt`. Docker Compose v2 is available. Results after implementation are below.

## D0-D2 (owner, gap filling, schema evolution)
- Owner identity is a CURRENT `person` with folded `relationship='self'`; `meta.self_uid` is repaired
  to the oldest match on startup/read. Conflicts are returned by `GET /api/self`. Only explicit
  API `force=true` bypasses owner protections; tools and capture do not pass force.
- Capture now resolves x-ref labels against all CURRENT records, proposes checked/undoable gap
  creates, and records direct operation dependencies. Apply returns 409 `dependency` rather than
  attempting an unresolved reference. Similar types remain unchecked; singular/plural-equivalent
  type proposals are dropped and records resolve to the existing type.
- Schema changes use migration 4, `entity_types.schema_version`, `entity_type_history`, the
  additive `POST /api/entity-types/{name}/extend` API, and capture operation undo. No dependency
  was added. Existing entity type PATCH behavior remains unchanged.
- UI and agent integration (D3) were not implemented; this task stops after D2.
- Verification: `pytest -q` (71 passed, 1 skipped), `ruff check .`, `git diff --check`, and
  `docker compose config --quiet` passed. `docker compose build` failed at
  `python -m hub.embeddings --download` because the sandbox could not resolve the Hugging Face
  host. Therefore the Compose image could not be started. A separate Python 3.11-slim Docker
  container with the repository mounted, `EMBED_BACKEND=fake`, and `VECTOR_BACKEND=numpy` returned
  `/health` status `ok`; this validates runtime startup only, not the production baked model or
  Compose image.

## UI capture E0-E1
- UI §0 inventory, baseline test/Compose results, actual curl response shapes, and the §2 API gap
  audit are recorded in `docs/UI_NOTES.md`. Existing API wrappers and IDs were retained.
- E0 adds `pytest-playwright` and Playwright as dev-only dependencies to implement the specified
  browser harness; no production dependency was added. Versions were checked against the GitHub
  Advisory Database before installation (no advisories reported).
- E0 fills the missing capture-list projection/filtering, add-properties apply override, dry-run
  validation, and batched entity-label API. Existing API operations (including captured plan
  normalization and apply checks) remain authoritative.
- This change stops at E1: plan operations are displayed read-only; selection, field editing,
  applying, undo UI, owner UI, and schema UI remain later sub-phases.
- Sandbox baseline Compose build/start failed at the existing model download because Hugging Face
  DNS resolution is unavailable. Repeat build and smoke results after changes are recorded below.
