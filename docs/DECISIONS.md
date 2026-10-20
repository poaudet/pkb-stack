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
- New dependencies `mcp==2.3.0` (official MCP Python SDK) and `httpx==0.28.1`, in
  `requirements-mcp.txt` only and installed by `Dockerfile.mcp`; the hub image does not
  include them. `mcp 2.3.0` pulled in `httpx2` rather than `httpx`, so `httpx==0.28.1` is explicitly
  pinned in `requirements-mcp.txt` for `hub_mcp/server.py` HTTP client calls.
  mcp 2.x renamed FastMCP to MCPServer; the bridge uses the low-level `Server`
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
- Verification after E1: `pytest -q` passed (75 passed, 1 skipped; 8 E2E tests excluded);
  `pytest -q -m e2e` passed (8 passed); `ruff check .`, `git diff --check`, and post-change curl
  checks passed.
- Docker CLI was present. Both `docker compose build` and `scripts/smoke.sh` were attempted after
  changes and failed at the existing `python -m hub.embeddings --download` step because the
  sandbox could not resolve the Hugging Face host. Compose could not start, so the Compose smoke
  API sequence was not run. The local Playwright E0/E1 suite did run successfully against the
  fake-embedding Flask app and canned LLM.

## UI capture E2-E3
- E2 adds schema-driven operation editing, dependency-aware selection, live `/validate` feedback,
  confirmation dialogs, apply results/links, undo reports, and answer-based regeneration. E3 adds
  owner setup and first-person gap flow, schema version/details/history/field extension, protected
  owner confirmation, Add-tab and Status integrations, and responsive action-bar/accessibility work.
- No dependency was added. The existing Playwright/Chromium harness and canned fixture server cover
  owner setup, H3 field dependencies/apply/undo, protected-owner confirmation, Add/Status integration,
  XSS text rendering, and the phone-width action bar.
- Verification: `pytest -q` passed (75 passed, 1 skipped; E2E deselected); `pytest -q -m e2e`
  passed (13 passed); `ruff check .`, `git diff --check`, and JavaScript module syntax checks passed.
- Docker CLI and Compose v2 are available, but `docker compose build` and `scripts/smoke.sh` both
  fail at the existing `python -m hub.embeddings --download` step because Hugging Face DNS is
  unavailable in the sandbox. The Compose app did not start, so its API smoke sequence is unverified.
- The physical-phone LAN check remains a manual verification for the operator; it cannot be
  performed from this sandbox.

## Selectable LLM endpoints
- `LLM_ENDPOINTS` (JSON array of `{name, base_url, model, api_key?}`) lists endpoints; the Settings
  tab picks one for tidy-up and one for capture (stored in `meta`, default first entry). When unset,
  the existing `LLM_*`/`CAPTURE_LLM_*` variables apply unchanged. API keys are never returned by
  `/api/settings/llm`. No dependency added.

## Dedicated Docker Compose configurations
- Added `docker-compose.hub.yml` containing only the `hub` service and persistent data volume for
  standalone Hub deployments without auxiliary profiles.
- Added `docker-compose.hub-mcp.yml` and `docker-compose.mcp.yml` containing both `hub` and `mcp`
  services, where `mcp` directly depends on `hub` (`depends_on: - hub`) and starts automatically
  without requiring the `mcp` compose profile.
- Maintained the existing `docker-compose.yml` with optional profiles for full-stack deployments
  (`mcp`, `local-llm`, `backup`).
- No new dependencies added.

## Hub improvements phases 1–5
- Capture visibility is the union of the `capture_visible` type flag and the
  `CAPTURE_EXCLUDE_TYPES` override. The default override keeps `place_visit` and
  application-owned PlainList types out of Capture.
- Client-supplied entity UIDs are accepted only by REST create; tools and Capture
  continue to generate IDs. The advertised feature token is `client_uid`.
- Entity `updated_at` remains UTC at whole-second precision for REST compatibility;
  writes advance it monotonically so the optional stale-write header detects concurrent
  edits even when two updates happen within the same second.
- Type deletion requires `confirm=true`; cascade cleanup runs through the caller's
  SQLite connection, including vector rows. Optional conversion creates one note per
  entity using a JSON representation before removing the records.
- Per-type policy flags are stored as integer columns and default to enabled. `searchable`
  is the API alias for `embed`; type/schema changes and destructive operations remain
  audited regardless of the `auditable` flag.
- No dependencies were added.

## Fixed entity-type schemas
- Added `allow_extension` as an entity-type policy, defaulting to `true` for existing and new
  types. When false, the shared extension validator rejects REST, Capture, and agent/MCP
  schema changes; visibility and record operations remain controlled by their separate flags.
- The policy can be configured at type creation or changed later through the existing type PATCH
  endpoint. No dependencies were added.

## Capture hardening A1 (redaction)
- Added `hub/capture/redact.py`; Capture text, hint and answers are redacted before gating,
  storage, and any LLM call. `captures.redacted_kinds` (migration 7) stores counts per kind only.
- Health card detection requires four uppercase letters plus 8–12 digits to limit false positives;
  passport, licence and bank-account patterns require their keyword before the token. The skip-AI
  note path also stores redacted text.
- The fixture `tests/fixtures/long_capture_recap.txt` was supplied later (synthetic data).
- No dependencies were added.

## Capture hardening A2-A8 and long capture (phase 6)
- Rejected ops use status `rejected` with `reject_reason` and `reject_message`; applying a plan
  with a rejected or invalid included op fails. Reasons: name_is_id, duplicate_candidate,
  target_not_candidate, owner_record_protected, not_a_habit. Planner bookkeeping goes to the log.
- Owner protection is narrower than the spec: the `SELF` alias may still be updated with
  non-financial content (existing SELF behaviour and gap filling depend on it). Any other path
  to the owner record, or financial content, is rejected.
- "Tuesday" from a Sunday capture date resolves to the next occurrence (2026-10-20) and the spec's
  expected values win over its "following day" wording. "this X" is on/after today; "next X" is in
  the following Monday-Sunday week. Health cards match uppercase letters only.
- The model reply is also redacted before parsing, so a model echo cannot reach storage.
- Gate "before" scores are the raw recorded bad reply without the pipeline (the old pipeline
  cannot be run). Task/habit/grocery types in the fixture world are test-defined schemas, since the
  PlainList schemas are not in the repository. `CAPTURE_EXCLUDE_TYPES` still excludes task and
  grocery types by default; clear it to capture them.
- The `captures.state` CHECK cannot be altered, so needs_review is `ready`, a cancelled job is
  `failed` with error `cancelled`, and a job with failed chunks is `failed` with a partial error.
  The API exposes `partial`. `/continue` moves a partial job to `ready`.
- Structured output tries json_schema, then json_object, then plain; every reply is validated and
  retried once, then the chunk fails with `schema_invalid`.
- Chunks run sequentially because each needs the working set of earlier chunks;
  `CAPTURE_LONG_CONCURRENCY` is parsed and reserved. Chunk offsets index the stored redacted text.
  The merged op cap is min(400, max ops x chunks). Provenance lives in `captures.op_meta`.
- Migration 8 adds `captures.mode/chunks_total/chunks_done/op_meta` and `capture_chunks`.
- New routes: POST `/api/capture/<id>/cancel`, `/continue`, `/chunks/<idx>/retry`.
- No dependencies were added.

## Capture long-text diagnostics (phase 1)
- Confirmed the existing lifecycle: `create` stores and queues a job; short jobs call
  `extract_plan` once; long jobs call it per chunk through `_run_chunk`, then finalize and merge.
  `extract_plan` requests structured output, redacts the reply before parsing, validates, then
  normalizes. A `ValueError` triggers one repair request.
- Confirmed the strict schema rejects summary length over 200, more than 5 questions, more than 20
  model ops, unsupported op names, invalid refs, and missing op ids. Normalization separately limits
  ops (including `keep_note`) and truncates questions. The short path and chunk path both cap
  `CAPTURE_MAX_OPS` at 20; the schema also hard-codes `ops.maxItems=20`.
- In this sandbox the three variables were unset: `CAPTURE_MAX_OPS=20`,
  `CAPTURE_CHUNK_TARGET_CHARS=3000`, and `CAPTURE_LONG_THRESHOLD_CHARS=4000` from Settings defaults.
  Thus an approximately 2,300-character input uses short mode automatically; forced long mode
  would make one chunk at the default target. The checked-in synthetic recap fixture is 1,700
  characters and is not the stated Appendix A input.
- Phase 1 adds value-free schema path/keyword issues, parse and operation-limit error categories,
  and safe persisted diagnostics. Failure logs include only diagnostic codes, issue/op counts,
  token counts, and finish reason. The current code now retains finish reason for this purpose.
- No capture LLM model is configured in the sandbox. Deterministic capture tests use the existing
  local stub; no live model call or live result was observed. Truncation, redaction corruption, and
  provider-specific schema behavior remain unconfirmed; these diagnostics do not change plan rules
  or operation limits.
