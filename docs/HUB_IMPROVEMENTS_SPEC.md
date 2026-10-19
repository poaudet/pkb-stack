# pkb-stack — Hub hardening and UI improvements (Spec H, for GitHub Copilot)

*Spec H v1 · 2026-10-18 · single owner · follows the earlier hub specs in `docs/`. Suggested path: `docs/HUB_IMPROVEMENTS_SPEC.md`. Written after PlainList, the owner’s todo and grocery app, moved its storage to this hub.*

## 0. Rules for the agent

1. Build what this spec says. “Decision” is final. When unsure, pick the simplest option and log it in `docs/DECISIONS.md`.
2. Work in the phases of §1, one PR each, and stop and report after each. A phase is done only when its “Done when” line passes. If you cannot run something (Docker, a real LLM), say so in the PR. Never claim something works untested.
3. Never invent function, class or route names. Read the code first (§2), run it, and fix failures instead of stubbing.
4. Follow the conventions already in the repo: the error envelope `{error:{code, message, …}}`, the migration mechanism, audit writes, test layout, linters. Do not refactor unrelated code.
5. Privacy: never log entity content, note text, captured text or tokens. Log ids, types, counts, durations and error codes only. In the UI render every value with `textContent` or an input’s `value`, never `innerHTML`, and keep the page free of inline scripts, inline styles and external assets so the Content-Security-Policy stays as it is.
6. Destructive operations (forget, type deletion) stay behind explicit confirmation and are not exposed to agents unless this spec says so.
7. Backward compatibility: API defaults do not change unless stated. PlainList (a REST client) and the MCP tools depend on today’s behavior. Every schema change is a migration, tested on a copy of an existing database.
8. In the same PR as the code, update the README, the API reference, `.env.example`, `docs/DECISIONS.md` and the chatbot skill (`SKILL.md` or equivalent) wherever they describe what changed.

## 1. Scope, order and why

PlainList now stores its data in this hub through the REST API, as four entity types: `task`, `grocery_list`, `grocery_item` and `plainlist_settings`. A small backend holds one hub token and calls `/api/entities*` and `/api/entity-types*`. That changes what the hub must guarantee:

- Those four types must never reach the capture LLM, and capture must never change them.
- The single hub token can touch every entity. Scoped tokens are the long-term fix (phase 7).
- Deleting or reshaping those types breaks the app, so destructive type operations need strong confirmation.
- The types are `embed: false`, written often, and need neither semantic search nor audit rows.
- PlainList is a REST client: API defaults must not change unless this spec says so.

The owner also asked for five improvements: hide archived entities in the UI (or filter them with a checkbox), edit an entity from the UI, delete an entity type from the UI, a long capture mode that splits a long input into many small entities, and an `extend_entity_type` tool for agents. Build in this order, one PR per phase:

| # | Phase | Why here |
| --- | --- | --- |
| 1 | Integration hardening: enforce capture exclusion everywhere; accept a client `uid` on create | Privacy gap first; the PlainList data migration needs the uid |
| 2 | UI: hide archived entities (checkbox to show them) and edit an entity | Small and visible; no schema change |
| 3 | Tool: expose `extend_entity_type` to agents | Small; unblocks the chatbot |
| 4 | Delete an entity type (API and UI) | Destructive; needs care, so after the easy wins |
| 5 | Per-type policy flags: `capture_visible`, `agent_visible`, `auditable` | Replaces scattered exclude lists |
| 6 | Long capture mode | Largest; builds on phases 1 and 5 |
| 7 | Scoped API tokens (optional) | Start only when the owner confirms |

## 2. Orientation (verify first; record findings in `docs/HUB_IMPROVEMENTS_NOTES.md`)

You have the repository; this spec was written from an earlier reading of it. Before coding, read the README, the specs in `docs/`, the API blueprints, the service layer, the capture package, the UI sources, the tool registry and MCP server, the migrations and the test layout. Run the whole test suite and record the baseline. Then confirm each assumption below and write confirmed or differs next to it. Where one differs, adapt the phase and log it in `docs/DECISIONS.md`.

1. Entity statuses are `current`, `superseded` and `archived`. `POST /api/entities/query` returns only `current`. `GET /api/entities` accepts a `status` filter.
2. `entity_types` has `embed`, `builtin`, `unique_key` and `schema_version`; extended properties carry `x-origin` and `x-added` markers.
3. Capture: `capture/context.py` builds the catalog (honors `CAPTURE_EXCLUDE_TYPES`) and the candidates (does not). `capture/plan.py` normalizes ops (does not reject ops on excluded types). Apply validates again.
4. `POST /api/entity-types/<name>/extend` exists and needs `confirm`. No tool wraps it.
5. There is no `DELETE /api/entity-types/<name>`.
6. The entity service accepts `uid` on create; the REST route does not pass it.
7. The UI is static JavaScript served under a Content-Security-Policy and renders entity rows from API JSON.
8. The tool registry serves `/api/tools/<name>` and the MCP server exposes the same tools; `TOOLS_ALLOW_DESTRUCTIVE` gates forget.
9. The service layer writes an `audit_log` row on every entity write.
10. Python is 3.11 or later, where `datetime.fromisoformat` accepts `Z` and fractional seconds.

## 3. Phase 1 — Integration hardening

**1.1 Capture exclusion must be enforced, not advisory.** Today (confirm in §2) an excluded type disappears from the catalog, but its entities still appear as candidates and the LLM can still propose ops on it. Required:

- One helper, `capture_excluded_types()`, returns the set from `CAPTURE_EXCLUDE_TYPES` (phase 5 adds the policy flag to the same helper). Every capture path uses it.
- Candidates: skip excluded types in both the deterministic label match and the semantic path. Excluded entities never appear in prompts, job records or the review UI.
- `normalize`: drop any create, update, supersede or extend op whose type, or whose target entity’s type, is excluded. Add the warning `type_excluded`; the job continues.
- Apply: re-check and refuse a plan containing such an op with `type_excluded`, because a plan can be edited in the UI or be stale.
- Config: document `CAPTURE_EXCLUDE_TYPES=place_visit,task,grocery_list,grocery_item,plainlist_settings` in `.env.example` and the README, with one sentence on why.
- Tests, with a stub LLM that records its requests: an excluded `task` titled Buy milk plus captured text that mentions it gives no candidate and no label in the prompt; a stub proposing `update_entity` on its id, or `create_entity` of type `task`, has the op dropped with `type_excluded`; a hand-edited plan sent to apply is refused.

**1.2 Client-supplied `uid` on create.**

- `POST /api/entities` accepts an optional `uid`: 32 lowercase hex characters. Malformed gives 400 `validation_error`. A `uid` already used by any entity, in any status, gives 409 `conflict`. Otherwise it goes to the service and `entity.id` equals it. Today unknown body keys are ignored silently, so an older hub returns a different id.
- REST only: agent tools never accept a `uid`.
- `GET /health` gains `features: ['client_uid']` so clients can detect support.
- Tests: create with a uid; duplicate; malformed; no uid unchanged.

**1.3 Regression checks for the PlainList contract** (change code only if one fails):

- Writes to an `embed: false` type never call the embedder and never answer `needs_reindex`.
- `date-time` accepts `2026-10-18T12:00:00.000Z`.
- Merge patch: `null` deletes a key; arrays are replaced whole.
- Query: `limit` is capped at 200, `total` ignores paging, and an `eq` filter on a string field works.
- Add `tests/test_plainlist_contract.py`: it creates small types shaped like PlainList’s (a `task` with `title`, a `state` enum and a `due_date` date-time; a `grocery_item` with a `list_id` pattern) and exercises create, query, patch and archive. Run it in every later phase.

Done when: excluded types are absent from every capture prompt and op path, a client uid works, and the contract test passes.

## 4. Phase 2 — UI: hide archived entities, edit an entity

**2.1 Archived entities are hidden by default.**

- Entity lists (per type, and any global or recent list), type counts and search results show only `current` entities by default. Confirm whether archived entities are searchable today; they must not appear in search unless the checkbox is on.
- A checkbox “Show archived” sits in each list toolbar. Checked: request `status=current,archived`. Archived rows are muted, carry an “Archived” badge, show `status_reason` as a tooltip, and have a Restore action (reuse the restore endpoint; add the button if it is missing, because hiding rows would otherwise make restoring hard).
- The choice is remembered in `localStorage` under `pkb.showArchived` (default false).
- API: if `GET /api/entities` accepts only one status, let `status` take a comma-separated list of known statuses (400 on an unknown one). With no `status` parameter the behavior is unchanged.
- Counts: `GET /api/entity-types` keeps `count` (current only) and adds `counts: {current, archived, superseded}`. With the checkbox on, the UI shows “N current · M archived”.
- A deep link to an archived entity still opens it, with a banner “Archived” and a Restore button.

**2.2 Edit an entity.**

- “Edit” appears on the entity detail and as a row action, for `current` entities only. Archived and superseded entities are read-only (Restore is offered).
- The form is generated from the type’s `json_schema` (fetched once, cached by `schema_version`): string becomes a text input, or a textarea when `maxLength` is over 120; `format: date` a date input; `date-time` a datetime-local input converted to and from ISO 8601 UTC with a trailing `Z`; `enum` a select; boolean a checkbox; number and integer a number input honoring `minimum` and `maximum`; an array of scalars a one-item-per-line list; a flat object a fieldset; anything else a JSON textarea checked with `JSON.parse`. Show required marks and each field’s `description` as help text. For `x-ref` fields offer a datalist filled from the existing labels endpoint (adapt to its real contract).
- Save sends `PATCH /api/entities/<uid>` with a minimal merge patch: changed fields only; a cleared optional field becomes `null` (which deletes the key); arrays are sent whole; a required field is never sent as `null`. If nothing changed, send nothing. Put the diff logic in a pure function with unit tests.
- Concurrency: the UI sends the header `X-Expected-Updated-At` with the `updated_at` it loaded. If the header is present and differs from the stored value, answer 409 `stale` with the current entity in `error.current`. The UI then offers Reload or Overwrite (resend without the header). Without the header nothing changes, so PlainList and other clients are unaffected.
- Errors: 400 maps to the field when the response names one (add `error.path` to schema validation errors if it is missing); 409 `conflict` (unique key) links to the existing entity; 409 `needs_reindex` shows a banner; a `protected` refusal (for example the owner record) shows its message and offers no retry.
- After a save, update the row and detail from the response; keep the scroll position and filters. The audit actor stays `ui`.
- Dialog accessibility: labels tied to inputs, focus trapped, Esc closes. It must work at phone width.
- Tests: API (`stale`, no-header compatibility, multi-status list, counts, unknown status 400); UI (form generation for every property kind, table-driven; the diff function).

Done when: archived entities are hidden by default and shown with the checkbox; an entity can be edited, with field errors and stale handling; no existing client behavior changed; tests and docs updated.

## 5. Phase 3 — Expose `extend_entity_type` as a tool

The REST route `POST /api/entity-types/<name>/extend` exists. Add the same capability to the tool registry and the MCP server as `extend_entity_type(name, add_properties, confirm=false)`.

- `confirm=false` (default) is a dry run: validate with the same rules as the REST route and return `{applied: false, diff: [added properties], schema_version}` or the refusal. `confirm=true` applies through the same service function and returns `{applied: true, schema_version, type}`.
- Do not duplicate or relax the rules; call the service. Expected rules (confirm): additive only; new properties are optional, never `required`; at most 5 properties per call; field names match the field-name pattern and do not collide; credential-like names are refused; only supported property types; each property needs a `description`; existing properties, `embed` and `unique_key` are untouched; added properties are stamped with `x-origin` (`agent:<name>`) and `x-added` (date).
- Idempotence: adding a property that exists with an identical definition is a no-op (`applied: false`, `reason: exists`); a different definition is 409 `conflict`.
- Gate: env `TOOLS_ALLOW_EXTEND` (default `true`). When false the tool answers `forbidden`. After phase 5 the tool also honors `agent_visible`.
- Audit: an `audit_log` row (actor `agent:<name>`) and the existing type history entry.
- Chatbot guidance: replace every statement that fields cannot be added with this flow: when the owner states a durable fact that has no matching field, call the tool with `confirm=false`, show the owner the diff, call it with `confirm=true` only after explicit approval, and otherwise keep the fact in a note. Update the tool descriptions, `SKILL.md` (or equivalent), the README tool table and any list of tools the MCP layer maintains by hand.
- Tests: dry run writes nothing; confirm applies once; every refusal above; the audit row; the tool appears in the MCP listing with its schema; `TOOLS_ALLOW_EXTEND=false` blocks it.

Done when: an agent can propose and, after approval, apply an additive extension through the tool, and all existing extension behavior is unchanged.

## 6. Phase 4 — Delete an entity type (API and UI)

**API**: `DELETE /api/entity-types/<name>?confirm=true` with optional `cascade=true`. Without `confirm` the answer is 400 `validation_error`.

- Refuse with 409 `dependency` (the body lists every reason) when: the type is builtin; another type’s schema references it through `x-ref` (`referenced_by: [types]`); a capture job is running; or the type has entities of any status and `cascade` is not true (the body gives `counts: {current, archived, superseded}`).
- With `cascade=true`, in one transaction: hard-delete every entity of the type together with its chunks, vectors, search rows and links (reuse the forget code path; check how the vector backend stores vectors and clean it too), then delete the type and its history. If the vector store cannot join the transaction, order the steps so a crash leaves only orphan vectors, and make the reindex or startup check remove orphans.
- Write one audit row `type_delete` with the type name and counts, never content.
- Not available to tools or MCP, and independent of `TOOLS_ALLOW_DESTRUCTIVE`.

**UI**:

- A “Delete type…” button in a danger zone on the type page, hidden for builtin types.
- The dialog shows the description in bold (an app may depend on the type), the counts per status and the referencing types. When blocked, it lists the reasons and offers nothing destructive. When only entities block it, it offers the cascade: the owner must tick “Also permanently delete N entities” and type the type name exactly. Offer “Export JSON” first (client-side, paging through `GET /api/entities?type=…`).
- On success: remove the type from the sidebar, return to the types overview and show a toast with the counts.

Tests: each refusal reason; cascade removes entities, chunks, vectors and search rows; the audit row has no content; builtin types are protected; a second delete answers 404; tools do not list a delete tool.

Done when: a custom type can be deleted safely from the UI, with every refusal and the cascade covered by tests.

## 7. Phase 5 — Per-type policy flags

Replace scattered exclude lists with explicit flags on each entity type, enforced in one place.

- Migration: add integer columns to `entity_types`: `capture_visible`, `agent_visible`, `auditable`, each default 1. The existing `embed` column stays and is presented as “searchable”.
- Semantics:
  - `capture_visible=0`: the type is excluded from the capture catalog, candidates, ops and apply (merged with `CAPTURE_EXCLUDE_TYPES` in the helper from phase 1, which stays as an override).
  - `agent_visible=0`: the type is invisible to tools and MCP: not listed, not returned by `query_entities`, `get_entity` or `search_knowledge`, and write tools answer `not_found`. The REST API and the UI are the owner’s own interfaces and are unaffected.
  - `auditable=0`: entity create, update, archive, restore and supersede writes for the type skip `audit_log` rows. The entity keeps `source`, `created_at` and `updated_at`. Schema and policy changes, forget and type deletion are always audited.
  - `embed=0`: not searchable, as today.
- API: `POST` and `PATCH /api/entity-types` accept and return `policy: {searchable, capture_visible, agent_visible, auditable}` (`searchable` is the `embed` alias); unknown keys are 400. Policy changes are not subject to the additive-schema rule.
- One module (`policy.py` or the repo’s equivalent) exposes `is_capture_visible`, `is_agent_visible` and `is_auditable`; capture, tools and the audit writer call it. No scattered checks. Filter by policy before ranking and limiting results, not after.
- UI: four toggles on the type page with plain-language help. Turning `auditable` off asks for confirmation because history stops.
- The owner applies this to the PlainList types after the merge: `embed=false`, `capture_visible=false`, `auditable=false`, `agent_visible=true`. Document it in the README.
- Tests: each flag enforced at every entry point (parameterize over the tools); audit skipped only for the right operations; the env override still works; the migration runs on a copy of an existing database.

Done when: the four flags exist, are enforced centrally, and are editable in the UI.

## 8. Phase 6 — Long capture mode

Goal: a long pasted text (a story, a day log, meeting notes, a trip recap, a voice dump) becomes a reviewable plan of many small entities instead of one overloaded LLM call. The capture rules still apply to every entry: one fact or occurrence per entry, the owner’s wording kept where it carries meaning, nothing inferred, and a sensitive detail left out entirely rather than stored in part.

**6.1 Trigger and limits**

- The existing capture route (confirm its name) accepts `mode`: `auto` (default), `short` or `long`. `auto` switches to long when the text is longer than `CAPTURE_LONG_THRESHOLD_CHARS`.
- New env variables, all in `.env.example`: `CAPTURE_LONG_MODE=auto` (or `off`), `CAPTURE_LONG_THRESHOLD_CHARS=4000`, `CAPTURE_LONG_MAX_CHARS=60000` (above it: 413 `too_large`, with the limit in the error), `CAPTURE_CHUNK_TARGET_CHARS=3000`, `CAPTURE_CHUNK_OVERLAP_CHARS=300`, `CAPTURE_LONG_MAX_CHUNKS=24`, `CAPTURE_LONG_CONCURRENCY=1`.
- Check the request-size limits in the app and in the deployment docs, and make sure the UI text area accepts the long text.

**6.2 Splitting (deterministic, no LLM)**

- Split on blank lines and headings, then on sentence ends. Pack pieces into chunks of at most the target size. Never cut inside a sentence; a single sentence longer than the target is its own chunk.
- Overlap: repeat the last paragraph of the previous chunk (up to the overlap size) at the start of the next one, as context only. Entities are extracted from the non-overlap part.
- Each chunk records `idx`, `start_char` and `end_char` (character offsets in the original), so the review can show where an entry came from.
- Property tests: chunks cover the whole text with no gaps; no chunk exceeds the target except the long-sentence case; splitting is stable for the same input.

**6.3 Extraction per chunk**

- Each chunk goes through the existing capture prompt with: the catalog (excluded types removed), candidates computed from that chunk, and a working set made of the entities proposed by earlier chunks of the same job (type, label, local id). The prompt says: this is chunk i of n; refer to the working set instead of proposing duplicates; extract only what the text states.
- Local op ids are made unique per job by prefixing the chunk index. Reuse the existing op-reference mechanism (confirm in `plan.py`).
- Rate limits: process chunks one after another by default. On 429 or 5xx back off (honor `Retry-After`), at most 3 attempts per chunk. A chunk that still fails is marked `failed` and the others continue.
- Exposure to the remote LLM is unchanged: only allowed types and labels as today, never an excluded type.

**6.4 Merge**

- Concatenate the chunk plans, then dedupe: two create ops with the same type and the same unique key or normalized label become one. Fields merge when they do not conflict; conflicting values stay as a `conflicts` warning for the reviewer.
- Rewrite references to the merged op ids; drop an op whose reference cannot be resolved and add a warning.
- Add one op that stores the full original text as a note (the existing note path). Record chunk provenance per op in job metadata, not in entity data.
- Run the merged plan through the existing `normalize` and validation once more. Respect the existing maximum number of ops; beyond it keep the first ones and add the warning `plan_truncated`.
- Privacy: apply the existing never-store rules to every op. A sensitive detail drops the whole op and records a warning without its content.

**6.5 Job model, API and progress**

- Migration: job fields `mode`, `chunks_total`, `chunks_done`; a table `capture_chunks(job_id, idx, start_char, end_char, status, plan_json, error_code)` with statuses `pending`, `running`, `done`, `failed`. Jobs and chunk results survive a restart.
- Job status reports progress. `POST /api/capture/<id>/cancel` stops after the current chunk. `POST /api/capture/<id>/chunks/<idx>/retry` redoes one failed chunk and re-merges. Adapt the route names to the existing ones.
- The job reaches `needs_review` when every chunk is done, or when the owner chooses to continue with the partial result.
- Apply and undo keep working per job, atomically, through `capture_ops`, as today.

**6.6 UI**

- Capture screen: a hint when the text is long (`Long capture: about N chunks`) and a toggle to force short or long.
- Progress view: a bar, per-chunk status, Cancel, and Retry on failed chunks.
- Review: ops grouped by chunk in collapsible sections; per-op accept or reject as today, plus accept all and reject all per chunk; merged duplicates and conflicts shown as warnings; the source note is the first op; each op links to its span of the original text.
- Render all text with `textContent`.

**6.7 Tests**

- Stub LLM, no network: splitter properties; merge and dedupe; cross-chunk reference rewrite; a failing chunk then a retry; cancel part-way; 429 backoff with a fake clock; privacy drop; excluded types absent from every chunk prompt; the full text preserved in the note; apply and undo of a long job.
- A 30,000-character fixture completes within the normal test time using the stub.

Done when: a long text produces one reviewable plan of small entities with progress, retry and undo, and short captures behave exactly as before.

## 9. Phase 7 — Scoped API tokens (optional; start only when the owner confirms)

Why: the PlainList backend holds a token that can read and change every entity in the hub.

- `API_TOKEN` stays as the full-access owner token, unchanged.
- Add named tokens with scopes. Storage: a table `api_tokens(name, token_sha256, scopes_json, created_at, last_used_at, disabled)`. The plaintext is shown once at creation and never stored. A CLI creates and manages them: `python -m hub.tokens create --name plainlist --types task,grocery_list,grocery_item,plainlist_settings --ops read,write`, plus `list` and `disable`.
- Scope model: `types` (allowed entity types) and `ops` (`read`, `write`, `archive`). A scoped token may call only `/api/entities*` for those types, and `/api/entity-types*` read-only. Tools, admin, capture, notes, search and self are refused unless granted. Everything not granted is 403 `forbidden`.
- Type confusion: for by-id calls, load the entity first and check its type against the scope before returning or changing it. Query and create check the requested type.
- Actor: for a scoped token the audit actor is the token name (for example `app:plainlist`), never the `X-Actor` header.
- Creating or altering types needs an explicit `entity_types:write` scope, off by default.
- Compare token hashes in constant time. Never log a token. Update `last_used_at` at most once a minute.
- Tests: every denial path; type confusion on `GET`, `PATCH` and archive; actor spoofing; a disabled token; the legacy token unchanged; a token never appears in logs.

Done when: PlainList can run on a token that can only see its four types, and the full-access token still works for everything else.

## 10. Cross-cutting requirements

- Migrations: one per phase that changes the schema, tested on a copy of a database created by the previous release; bump the schema version as the repo does.
- Tests: each phase adds tests next to the existing ones, runs offline (stub LLM, fake embedder) and keeps the whole suite and the linters green.
- Security review per phase: new inputs validated; no content in logs; destructive routes require `confirm`; UI values rendered with `textContent`; CSP unchanged.
- Docs: README, API reference, `.env.example`, `docs/DECISIONS.md` and the chatbot skill updated in the same PR, listing new endpoints, environment variables, tools and UI behavior.
- Compatibility: run the PlainList contract test (phase 1.3) in every phase.
- Performance: with “Show archived” off the UI must not fetch archived rows; long capture runs as an asynchronous job like today and never holds a request open.

## 11. Pitfalls

- Adding multi-status to `status` must not change the default for existing clients (PlainList, tools).
- Hiding archived rows hides the obvious way to restore them: keep the checkbox, the Restore action and working deep links.
- Editing in the UI skips capture review. Server validation is the only guard; never trust the generated form.
- `date-time` values are ISO 8601 UTC with `Z`; datetime-local inputs convert on the way in and out.
- In a merge patch `null` removes the key. Never send `null` for a required field.
- Type deletion with cascade must also clean vectors, search rows and chunk tables; check the vector backend.
- Deleting `task`, `grocery_list`, `grocery_item` or `plainlist_settings` breaks PlainList: show the description and require the typed name.
- The extension tool lets an agent change a shared schema: keep the dry run as default, additive-only and the audit row.
- `auditable=0` removes history: type-level and destructive operations stay audited.
- Filter by policy before ranking and applying a limit, or hidden types can starve the results.
- Long capture: do not extract from the overlap text twice; keep job state in the database; free-tier LLMs rate-limit, so concurrency stays 1 by default.
- A client-supplied `uid` is accepted only on the REST create route, never from tools or the capture path.
- Scoped tokens: `GET /api/entities/<uid>` returns any type today, so the scope check must run after loading the entity.

## 12. Owner decisions (defaults chosen)

- **D1** Phases run in the order of §1; phase 7 waits for the owner’s confirmation.
- **D2** The edit form uses PATCH. Keeping history through supersede is not part of this spec.
- **D3** “Show archived” defaults to off and is remembered per browser.
- **D4** `extend_entity_type` is on by default (`TOOLS_ALLOW_EXTEND=true`) and applies only with `confirm=true`.
- **D5** Type deletion is blocked for builtin and referenced types; with entities it needs the explicit cascade and a typed name, and is never exposed to tools.
- **D6** Policy flags are columns, not JSON; `embed` stays as the searchable flag.
- **D7** Long capture is automatic above 4,000 characters and sequential by default; limits live in environment variables.
- **D8** Optimistic concurrency uses the optional header `X-Expected-Updated-At` on PATCH.
- **D9** `CAPTURE_EXCLUDE_TYPES` stays as an override merged with the policy flag.
