# UI capture implementation notes

## Preconditions and baseline

Audited the assumptions in `BUILD_SPEC.md`, `CAPTURE_SPEC.md`,
`SELF_GAPS_SPEC.md`, and this document's parent `UI_CAPTURE_SPEC.md` before
starting E0.

- Capture C0-C2 are present: `hub/capture/` contains gates, context, prompt,
  LLM, plan, apply, and job modules; migrations create `captures`,
  `capture_ops`, and `capture_usage`; tests cover apply and undo.
- BUILD_SPEC prerequisites are present: the OpenAI-compatible client in
  `hub/llm.py` has JSON fallback/parsing; entity create/update/supersede and
  type validation exist; `hub.queue` uses `ThreadPoolExecutor` and startup
  recovery; `hub.db.connect` registers deterministic `fold()` on every
  application connection.
- SELF_GAPS prerequisites are present: `entities.render_text` and
  `entities.validate_data` handle entity rendering/validation; entity-type
  PATCH enforces additive schema changes; `capture.apply` detects unresolved
  references/dependencies; `capture_ops.kind` has no restrictive CHECK.
- UI inventory: `hub/static/index.html`, `app.js`, and `style.css` make up the
  current vanilla-JS UI. `app.js` registers tabs directly, uses a
  `createElement`/`textContent` helper, and has one fetch wrapper that reads
  `hub_token` from localStorage, prompts once on 401, and sends `X-Actor: ui`.
  CSS uses light/dark variables, sticky top navigation, and mobile-width
  controls. Review, Entities, and Status fetch/render directly in `app.js`.
- Baseline `pytest -q`: **71 passed, 1 skipped**.
- Baseline `docker compose up -d --build`: **failed before startup** at
  `python -m hub.embeddings --download`; the sandbox could not resolve the
  Hugging Face host (`httpx.ConnectError`, DNS). No Compose containers were
  running.
- The existing `scripts/stub_llm.py` supports a fixed default G1 response and
  an optional single `--reply-file`; it has no scenario matching or sequence
  of replies. There was no Playwright test harness.

## API shapes checked with curl

Used a temporary app with `EMBED_BACKEND=fake`, `VECTOR_BACKEND=numpy`, a
temporary data directory, and the repository's stub LLM. IDs below are
opaque hex strings; the GET response is wrapped in `capture`.

| Request | Observed response |
| --- | --- |
| `GET /api/capture/config` | `{enabled, model, host, local, max_chars, calls_today, daily_limit, resets_at}` |
| `GET /api/capture?limit=2` | `{items, total}`; items are full capture objects |
| `POST /api/capture` | HTTP 202, `{capture: {id, state, text, plan, ...}}` |
| `GET /api/capture/{id}` | `{capture: {id, state, plan, ...}}`; plan keys include `summary, ops, questions, warnings, keep_note, note, gaps` |
| `POST /api/capture/{id}/regenerate` | HTTP 202, `{capture: {...}}` |
| `POST /api/capture/{id}/apply` | `{applied, skipped}` |
| `POST /api/capture/{id}/undo` | `{undone, kept_modified, kept_in_use, types_left}` |
| `GET /api/self` | `{self, configured, conflicts}` |
| `GET /api/entity-types` | `{types}`; each item includes `json_schema, schema_version, builtin, unique_key, description` |
| `POST /api/entity-types/{name}/extend` | `{type, version, added}` |
| `GET /api/entity-types/{name}/history` | `{type, history}` |

The Capture plan has `ops` with both `kind` and `op`; op IDs are strings.
Observed op fields include `id, op, kind, type, label, why, status, include,
errors, warnings, diff`, and may include `target, data, patch, risk, requires,
gap`.

## E0 API gap inventory

- Already available: config, create/get, state-filtered list, regenerate,
  apply, undo; self GET/PUT; type list/get/extend/history; entity and note
  lookup by ID; protected-owner archive/forget query flags.
- Incomplete: capture listing lacks `source`, `summary`, and `op_count`
  filtering/projection; apply has no `add_properties` op override; there is no
  no-write `/validate` endpoint or batched entity labels endpoint.
- E0 will fill only the missing capture list, add-properties override,
  validate, and entity-label behaviors while preserving existing response
  wrappers and service validation. E1 consumes the already available config,
  create, get, save-note, and list endpoints.
