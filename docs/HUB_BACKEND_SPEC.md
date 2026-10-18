PlainList on the hub — Build Spec (for GitHub Copilot)
Spec v1 · 2026-10-17 · single owner · replaces the S3 storage layer of infra/backend with the pkb-stack REST API. Suggested path: docs/HUB_BACKEND_SPEC.md. Self-contained: you have no access to the hub’s repository, so §4 is the hub contract.
0. Rules for the agent
1. Build what this spec says. “Decision” is final. When unsure, pick the simplest option and log it in docs/DECISIONS.md (create it if missing).
2. Work in the phases of §12, one PR each, and stop and report after each. A phase is done only when its Definition of Done passes. If you cannot run something (wrangler, Docker, a real hub), say so in the PR. Never claim something works untested.
3. Never invent library APIs. Read the installed version’s docs or types (Hono, zod, vitest, wrangler, Angular), run the code, and fix failures instead of stubbing.
4. No new runtime dependency: the hub client uses fetch. A dev dependency needs a one-line reason in DECISIONS.md.
5. Follow the repo config. Backend and shared extend tsconfig.base.json: strict, exactOptionalPropertyTypes, noUncheckedIndexedAccess (never assign undefined to an optional property; use conditional spreads). ESLint: interface for object shapes, no any. Prettier: single quotes, trailing commas, width 100. pnpm run lint, pnpm run test and pnpm run build must pass.
6. Never log entity content, tokens, header values or hub error messages. Log method, path, status, duration and error code only.
7. The backend may call only the hub endpoints in §4, and only for the four types in §6. No generic proxy. You have no access to the hub’s code: §4 is the contract. Where the real hub differs from it, follow the hub, note the difference in docs/HUB_NOTES.md and tell the owner in the PR.
1. What changes and why
The hub (pkb-stack) becomes the single AI-ready store. Todos and grocery lists become entity types in it, so a chatbot (the hub’s MCP and tools) can read and update them next to the owner’s long-term knowledge. PlainList keeps its UX and its local-first behavior.
Angular app (local-first, same sync algorithm)
   | Authorization: Bearer PLAINLIST_API_TOKEN
   v
PlainList backend (Hono on Workers or Node) = facade
   |-- auth + CORS
   |-- routes: same paths and shapes as today
   |-- repositories + mappers (ids, field names, defaults)
   '-- HubClient (fetch)
         | Authorization: Bearer HUB_API_TOKEN, X-Actor: plainlist
         v
pkb-stack: /api/entities*, /api/entity-types*
Decisions:
• The backend stays as a facade. Routes and request/response shapes stay what the Angular app sees today; only the storage behind them changes (S3 collection files become hub entities).
• The hub token is a server-side secret. The browser never sees it and never talks to the hub.
• The sync algorithm (three-way merge) is not rewritten. §9 adds one safety guard.
• One owner per deployment. user_id query parameters are accepted for compatibility and ignored.
• S3 leaves the runtime. A one-off migration script keeps a copy of the S3 client (§10).
• Deleting in PlainList archives in the hub (reversible). The backend never calls the hub’s forget, supersede or restore endpoints.
Non-goals: scoped hub tokens and per-type policy flags (hub side, §3), mobile app, realtime sync, multi-user, rewriting the sync algorithm.
2. Preconditions (verify first; write findings to docs/HUB_NOTES.md)
1. Inventory infra/backend (routes, storage, config, health), apps/web (plainlist-api.service.ts, plainlist.service.ts, the settings page) and packages/shared (schemas, healthResponseSchema, configS3Schema). Run lint, test and build. Fix nothing; report.
2. Ask the owner for a throwaway hub URL and token. If you get them, record the real response shape of every call in §4 with curl and compare it with the reference. Where they differ, follow the hub and note it. If you get none, build against §4 and the fake hub of §11, and say so in the PR.
3. Check how the installed Hono exposes environment variables on Workers and on @hono/node-server. Prefer the env() helper from hono/adapter if it covers both. Verify with wrangler dev and pnpm --dir infra/backend start:selfhost.
4. Write a test that shows whether GET /tasks/search and GET /grocery-items/search are reachable today. They are registered after the :id routes and look shadowed.
5. Check the current Workers subrequest limit for the plan in use (§13).
3. Assumptions about the hub (owner-side work; not part of this repo)
The hub is a separate service, called pkb-stack, that the owner maintains in another repository. You have no access to its code. §4 is the contract you build against. The owner is responsible for:
• Capture exclusion. The hub has an LLM capture pipeline that turns free text into entities. The owner configures it to ignore the four PlainList types (§6) and closes any gaps in that exclusion. You do nothing for this.
• Client-supplied ids. POST /api/entities is expected to honor an optional uid once the owner confirms the change. Only the migration (§10) needs it, and the script must verify it: the returned id must equal the requested uid, otherwise abort.
• Later: scoped tokens and per-type policy flags (searchable, capture-visible, auditable). Until then the single hub token can read and write every entity in the hub, not only PlainList’s. That is why the allowlist (rule 7 in §0), the type check and the id validation (both in §7) are mandatory.
If the real hub behaves differently from §4, follow the hub, record the difference in docs/HUB_NOTES.md and tell the owner in the PR.
4. Hub API reference (OpenAPI-style subset)
This is everything you may call. It is an OpenAPI 3.1 fragment plus the behavior a schema cannot express. Treat anything not listed here as forbidden. Verify it against a real hub when the owner gives you a URL and a token (§2).
openapi: 3.1.0
info:
  title: pkb-stack hub API (subset used by PlainList)
  version: '1'
servers:
  - url: '{HUB_URL}'            # from env, no trailing slash
security:
  - bearerAuth: []
components:
  securitySchemes:
    bearerAuth: {type: http, scheme: bearer}   # the hub's static API token
  parameters:
    XActor:
      name: X-Actor
      in: header
      schema: {type: string, maxLength: 80}
      description: Caller label stored in the hub audit log. Always send 'plainlist'.
  schemas:
    Entity:
      type: object
      required: [id, type, data, source, status, created_at, updated_at]
      properties:
        id: {type: string, pattern: '^[0-9a-f]{32}$'}
        type: {type: string}
        data: {type: object, description: Validated against the type's json_schema}
        source: {type: string, description: 'ui, api or agent:<name>'}
        status: {type: string, enum: [current, superseded, archived]}
        superseded_by: {type: [string, 'null']}
        status_reason: {type: [string, 'null']}
        created_at: {type: string, description: 'UTC, whole seconds, e.g. 2026-10-17T12:00:00Z'}
        updated_at: {type: string, description: same format}
    EntityType:
      type: object
      required: [name, json_schema]
      properties:
        name: {type: string, pattern: '^[a-z][a-z0-9_]{0,39}$'}
        description: {type: string}
        json_schema: {type: object, description: JSON Schema 2020-12, see Type rules}
        text_template: {type: string, description: 'Placeholders like {title}'}
        unique_key: {type: [string, 'null'], description: Top-level property, case-insensitively unique among current entities}
        embed: {type: boolean, default: true}
        schema_version: {type: integer, readOnly: true}
        count: {type: integer, readOnly: true}
    QueryRequest:
      type: object
      required: [type]
      properties:
        type: {type: string}
        filters:
          type: array
          items:
            type: object
            required: [field, op]
            properties:
              field: {type: string, description: Must be a property of the type's schema}
              op: {type: string, enum: [eq, ieq, ne, gt, gte, lt, lte, contains, in, exists, has], description: PlainList uses only eq}
              value: {}
        order_by: {type: object, properties: {field: {type: string}, dir: {type: string, enum: [asc, desc]}}}
        limit: {type: integer, minimum: 1, maximum: 200, description: Always send it explicitly}
        offset: {type: integer, minimum: 0, default: 0}
    Error:
      type: object
      required: [error]
      properties:
        error: {type: object, required: [code, message], additionalProperties: true}
paths:
  /health:
    get:
      summary: Liveness. No auth. Proves nothing about the token.
      security: []
      responses: {'200': {description: ok}}
  /api/entities:
    post:
      summary: Create an entity
      parameters: [{$ref: '#/components/parameters/XActor'}]
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [type, data]
              properties:
                type: {type: string}
                data: {type: object}
                uid: {type: string, pattern: '^[0-9a-f]{32}$', description: Optional client id. Honored only after the owner confirms (section 3). An older hub ignores it and returns a different id}
      responses:
        '201': {description: 'Created. Body {entity: Entity}'}
        '400': {description: validation_error (unknown type, data fails the type schema, malformed uid)}
        '401': {description: unauthorized}
        '409': {description: 'conflict. unique_key already used (error.existing is the id of the existing entity) or uid taken'}
        '413': {description: too_large}
  /api/entities/{id}:
    parameters:
      - {name: id, in: path, required: true, schema: {type: string, pattern: '^[0-9a-f]{32}$'}}
    get:
      summary: Read one entity of ANY type and ANY status. The caller must check type and status.
      responses:
        '200': {description: 'Body {entity: Entity}'}
        '404': {description: not_found}
    patch:
      summary: Update by JSON Merge Patch (RFC 7386)
      description: The body is the bare patch object, not wrapped. null deletes a key, objects merge recursively, arrays and scalars are replaced whole. The merged document is re-validated against the type schema.
      parameters: [{$ref: '#/components/parameters/XActor'}]
      requestBody: {required: true, content: {application/json: {schema: {type: object}}}}
      responses:
        '200': {description: 'Body {entity: Entity}'}
        '400': {description: validation_error}
        '404': {description: not_found}
        '409': {description: conflict (for example a unique_key clash)}
  /api/entities/query:
    post:
      summary: List entities of ONE type
      description: Returns only entities with status current. Default order is newest first. total counts all matches and ignores limit and offset.
      requestBody: {required: true, content: {application/json: {schema: {$ref: '#/components/schemas/QueryRequest'}}}}
      responses:
        '200': {description: 'Body {items: Entity[], total: integer}'}
        '400': {description: validation_error (unknown type or unknown filter field)}
  /api/entities/{id}/archive:
    post:
      summary: Soft delete. The data is kept and the hub owner can restore it.
      parameters: [{$ref: '#/components/parameters/XActor'}]
      requestBody: {required: false, content: {application/json: {schema: {type: object, properties: {reason: {type: string}}}}}}
      responses:
        '200': {description: 'Body {entity: Entity} with status archived'}
        '404': {description: not_found}
        '409': {description: conflict (already archived)}
  /api/entity-types:
    get:
      summary: List all types
      responses: {'200': {description: 'Body {types: EntityType[]}'}}
    post:
      summary: Create a type (the body is an EntityType)
      responses:
        '201': {description: 'Body {type: EntityType}'}
        '400': {description: validation_error (see Type rules)}
        '409': {description: conflict (type exists)}
  /api/entity-types/{name}:
    get:
      summary: Read one type
      responses:
        '200': {description: 'Body {type: EntityType}'}
        '404': {description: not_found}
    patch:
      summary: Additive schema change. Body {json_schema}.
      responses:
        '200': {description: 'Body {type: EntityType}'}
        '409': {description: conflict (the change is not additive)}
Behavior the schema cannot express:
• Ids are 32 lowercase hex characters. Timestamps are UTC with whole seconds. A format: date-time value must be ISO 8601 UTC with a trailing Z.
• Auth failures answer 401 unauthorized. Error envelope: {error: {code, message, ...}}. Codes: validation_error 400, unauthorized 401, not_found 404, conflict 409, needs_reindex 409, too_large 413, internal 500.
• There are no transactions, no batch endpoint and no ETag or If-Match. Updates are last-write-wins: do read-modify-write yourself.
• Types with embed: false are never indexed or embedded, and writes to them never call an embedding service. Types with embed: true can answer 409 needs_reindex while the owner re-indexes. PlainList types are embed: false; if you see needs_reindex, map it to 502.
• Every write is recorded in the hub audit log with the X-Actor value.
• The hub has other endpoints (notes, search, capture, agent tools, admin, delete, supersede, restore). PlainList must never call them.
Type rules (enforced when creating or altering a type):
• name matches ^[a-z][a-z0-9_]{0,39}$. json_schema is an object schema with 1 to 50 top-level properties, and field names match ^[A-Za-z_][A-Za-z0-9_]*$.
• Property types: string (with format date or date-time), number, integer, boolean, arrays of scalars, flat objects (depth 2). required, enum, minLength, maxLength, minimum, maximum, exclusiveMinimum and pattern are validated on every write. Unknown keywords such as x-ref are ignored by validation and kept as hints.
• unique_key must name a top-level property.
• Altering a type is additive only: adding optional properties is accepted; changing or removing a property, or adding a required one, answers 409.
Examples (paths and bodies only; headers as above):
POST /api/entities
{"type": "task", "data": {"title": "Buy milk", "state": "pending", "priority": "medium"}}
-> 201 {"entity": {"id": "3f2a9c1e8b7d4a6f9e0c5d1b2a3f4e5d", "type": "task", "data": {...}, "source": "api", "status": "current", "superseded_by": null, "status_reason": null, "created_at": "2026-10-17T12:00:00Z", "updated_at": "2026-10-17T12:00:00Z"}}

POST /api/entities/query
{"type": "grocery_item", "filters": [{"field": "list_id", "op": "eq", "value": "3f2a9c1e8b7d4a6f9e0c5d1b2a3f4e5d"}], "limit": 200, "offset": 0}
-> 200 {"items": [Entity, ...], "total": 37}

PATCH /api/entities/3f2a9c1e8b7d4a6f9e0c5d1b2a3f4e5d
{"state": "completed", "notes": null}
-> 200 {"entity": {...}}   (state replaced, notes removed, other fields untouched)

POST /api/entities/3f2a9c1e8b7d4a6f9e0c5d1b2a3f4e5d/archive
{"reason": "deleted in PlainList"}
-> 200 {"entity": {..., "status": "archived"}}

Any failure
-> 404 {"error": {"code": "not_found", "message": "entity not found"}}
5. Configuration
# Backend. Worker secrets or Node env. Never commit real values.
HUB_URL=                # required. Hub base URL, no trailing slash
HUB_API_TOKEN=          # required. The hub's API_TOKEN. Secret
HUB_ACTOR=plainlist     # X-Actor value
HUB_TIMEOUT_MS=10000    # per hub call
HUB_EXTRA_HEADERS=      # optional JSON object, e.g. Cloudflare Access service-token headers. Secret
PLAINLIST_API_TOKEN=    # required. Bearer token the Angular app must send. Secret
CORS_ORIGINS=http://localhost:5173   # comma-separated list; a wildcard only when NODE_ENV=development
LLM_BASE_URL=           # optional server-side defaults for /parse; request values win
LLM_MODEL=
LLM_API_KEY=            # secret
• Read configuration in one place (loadConfig(c), validated with zod). Missing required values make every data route answer 503 {error:'backend_not_configured', missing:[names]} while /health keeps working (names only, never values).
• HUB_EXTRA_HEADERS must be a JSON object with string values. Reject CR or LF in names or values.
• Workers: set every HUB_*, PLAINLIST_API_TOKEN and LLM_API_KEY with wrangler secret put or the dashboard. No hub hostname in wrangler.toml or anywhere else in the repo. Local dev: .dev.vars (add it to .gitignore) and a Node .env.
• Update .env.example with the block above. Rewrite the README “Runtime variables” section: S3 is gone, the hub variables are deployment secrets, and LLM settings are per request with server-side defaults.
6. Entity types
Four types, created by pnpm --dir infra/backend hub:bootstrap. Keep the definitions in one file, src/hub/types.ts. Build each json_schema from the tables below: type: object, required as listed, no additionalProperties rule.
• All four use embed: false: no embedding call on write, no semantic search, and immunity to the hub’s needs_reindex state.
• The task field is state, not status, to avoid confusion with the hub’s entity-level lifecycle status.
• No unique_key except on settings. Two devices that each create “Weekly groceries” offline must not collide on a 409.
• Field descriptions are part of the contract: the chatbot reads them.
task: template {title} [{state}] due {due_date}. {notes}; required title.
Field
Schema
Description
title
string, 1–200 chars
Short name of the task.
notes
string, ≤ 1000
Extra details.
state
string enum: pending, in_progress, completed, archived
Progress. archived is a PlainList state, unrelated to the hub’s archive lifecycle. Default pending.
priority
string enum: low, medium, high
Importance. Default medium.
time_slot
string enum: morning, afternoon, evening, anytime
Preferred time of day.
duration_minutes
integer, 1–1440
Estimated minutes.
due_date
string, format date-time
Due moment, ISO 8601 UTC.
grocery_list: template {name}. {description}; required name.
Field
Schema
Description
name
string, 1–200 chars
List name.
description
string, ≤ 500
What the list is for.
is_archived
boolean
PlainList flag for lists put away. Unrelated to the hub’s archive lifecycle.
grocery_item: template {name} {quantity} {unit} ({department}); required list_id, name.
Field
Schema
Description
list_id
string, pattern ^[a-f0-9]{32}$, x-ref: grocery_list
Id of the grocery_list this item belongs to.
name
string, 1–200 chars
Item name.
quantity
number, exclusiveMinimum 0
How many or how much.
unit
string, ≤ 50
Unit, for example kg, liter, piece.
department
string, 1–100 chars
Store section. Any text; other when unknown.
purchased
boolean
True once bought.
notes
string, ≤ 500
Extra details.
plainlist_settings: unique_key: name; template {name}; required name. One record named default. Non-secret values only.
Field
Schema
Description
name
string
Always default.
instruction
string, ≤ 5000
System instruction sent with every AI parse request.
todo_prompt, grocery_prompt
string, ≤ 2000
Prompts for parsing text into tasks and grocery items.
timezone
string, ≤ 100
IANA timezone, for example America/Toronto.
region_preference
string, ≤ 100
Region preference.
llm_base_url
string, ≤ 500
OpenAI-compatible base URL. No key.
llm_model
string, ≤ 200
Model name.
Shape of a create-type call (grocery_list shown):
{
  "name": "grocery_list",
  "description": "A named shopping list. Items belong to a list through grocery_item.list_id.",
  "embed": false,
  "text_template": "{name}. {description}",
  "json_schema": {
    "type": "object",
    "required": ["name"],
    "properties": {
      "name": {"type": "string", "minLength": 1, "maxLength": 200, "description": "List name."},
      "description": {"type": "string", "maxLength": 500, "description": "What the list is for."},
      "is_archived": {"type": "boolean", "description": "PlainList flag for lists put away. Unrelated to the hub's archive lifecycle."}
    }
  }
}
Bootstrap rules:
• For each type: GET /api/entity-types/{name}; on 404 POST /api/entity-types. If it exists and every property of the spec is present and identical, do nothing. If only new properties are missing, PATCH additively. Anything else (a changed definition, embed not false): print a diff and exit 1. Never use ?force=true.
• Idempotent: a second run changes nothing and exits 0.
7. Mapping between the app and the hub
Ids. Hub ids are 32 hex characters; the app uses dashed UUIDs (8-4-4-4-12). Convert at the boundary only (toExternalId, toHubId; lowercase on input). A path or body id that is not a well-formed dashed UUID answers 404 with zero hub calls. The backend holds the hub token, so an unvalidated id such as ../admin/backup would be a path injection. grocery_item.list_id stores the hub hex.
Type check. Every by-id read or write fetches the entity first and requires entity.type to be the expected type and status to be current. Otherwise answer 404 and write nothing. Without this, /tasks/{id of a person} would expose or modify any hub entity, the owner record included.
Field map.
App
Hub
id
entity id (dashed)
createdAt
created_at
updatedAt, last_modified
updated_at
Task title, notes, priority
same names
Task status
state
Task timeSlot, durationMinutes, dueDate
time_slot, duration_minutes, due_date
List name, description
same names
List isArchived
is_archived
Item listId
list_id (dashed to hex and back)
Item name, quantity, unit, department, purchased, notes
same names
Settings instruction, timezone
same names
Settings regionPreference, prompts.todo, prompts.grocery
region_preference, todo_prompt, grocery_prompt
Settings llm.baseUrl, llm.model
llm_base_url, llm_model
Rules.
1. Timestamps. The app compares updatedAt as strings against its own millisecond clock, and the hub truncates to whole seconds. Output created_at as …SS.000Z and updated_at as …SS.999Z (the last millisecond of that second). Otherwise an item written in the same second as the local edit always looks older than the local copy and is pushed again on every sync.
2. Defaults on read: state becomes pending, priority becomes medium, department becomes other, purchased and is_archived become false. Agent-created records may omit them.
3. user_id echoes the query parameter or default-user; is_deleted is always false; conflict_token is omitted.
4. Omitted JSON keys are never sent as null (unchanged behavior: the app cannot clear an optional field today).
5. Every response must satisfy the shared zod schemas (taskSchema, groceryListSchema, groceryItemSchema). Test it.
8. Backend changes (infra/backend)
8.1 Layout (suggested)
src/
  env.ts                    loadConfig(c)
  auth.ts                   bearer check + CORS
  hub/client.ts             HubClient
  hub/types.ts              §6 definitions
  hub/ids.ts, mappers.ts    §7
  hub/repos/{tasks,lists,items,settings}.ts
  routes/…                  same paths as today (replaces r2-routes.ts and config-routes.ts)
scripts/
  hub-bootstrap.ts, migrate-s3-to-hub.ts, legacy/s3-store.ts
8.2 Auth and CORS
• Every route except GET / and GET /health requires Authorization: Bearer <PLAINLIST_API_TOKEN>. Compare in constant time with WebCrypto only (SHA-256 both values, XOR-compare the digests) so it runs on Workers and Node. Missing or wrong: 401 {error:'unauthorized'}. Token not configured: 503 (fail closed).
• worker.ts has two CORS blocks (hono/cors and a manual one that sets *). Replace both with one middleware driven by CORS_ORIGINS. Allowed headers: Content-Type, Authorization, X-Requested-With; drop X-PlainList-S3-Config. Answer OPTIONS with 204 before the auth check. CORS is defense in depth; the bearer token is the real gate.
8.3 HubClient
• new HubClient({baseUrl, token, actor, timeoutMs, extraHeaders, fetchImpl?}). fetchImpl is injectable for tests.
• Timeout with AbortController. Retry once on reads only (GET and query), on a network error or 502/503/504. Never retry writes.
• Error mapping: timeout becomes 504 hub_timeout; network failure 502 hub_unreachable; hub 401 becomes 502 hub_auth_failed (log at error level, no detail); hub 404 stays 404; hub 409 conflict stays 409; hub 400 validation_error becomes 502 hub_schema_mismatch (the request already passed our zod validation, so a hub rejection means type drift); other 5xx become 502.
• queryAll(type, filters, {limit, offset}) pages through the hub in chunks of 200.
• Errors and logs never contain the token or header values.
8.4 Routes
Same paths, methods and bodies as today, implemented over the repositories.
Route
Behavior over the hub
GET /tasks, GET /grocery-lists
queryAll; body {items, total, limit, offset} as today
GET /grocery-lists/:id/items
queryAll with list_id eq <hex id>
GET /tasks/status/:status
queryAll with state eq <status>
GET /tasks/search, GET /grocery-items/search
Read everything (paged) and filter in memory like today: title and notes for tasks; name and notes, plus listId, for items. Register literal routes before the :id routes
POST /tasks, /grocery-lists, /grocery-items
Validate with the existing shared request schemas, map, create, answer 201. For an item the list must exist and be current, else 400 Invalid grocery item payload. with message List <id> does not exist.
POST /tasks/batch, /grocery-items/batch
Sequential creates, same {created, failed, errors[]} body; more than 25 entries answer 400 (subrequest limit, §13)
GET /tasks/:id, /grocery-lists/:id, /grocery-items/:id
§7 type check, then map
PUT on the same three
Read (type and status check), build the minimal patch against the stored data, skip the hub write when nothing changed, otherwise PATCH, return the mapped result. Changing an item’s list validates the target list
DELETE on the same three
Archive with reason deleted in PlainList; 200 {success:true}; 404 if missing, of another type or already archived (the app ignores 404). Deleting a list also archives its items (sequential, best effort)
POST /grocery-lists/:id/merge-items
Same semantics as today: quantities summed (a missing one counts as 0), notes joined with a vertical bar. PATCH the target first, then archive the source; if the second step fails answer 502 and keep both, so no data is lost
Skipping no-op writes avoids a hub audit row and an updated_at bump for data that did not change.
8.5 Config route
• GET /config and PUT /config keep today’s shapes minus s3. Data lives in the plainlist_settings record named default, created by the first PUT. If two creates race, the hub’s 409 carries existing: retry as a PATCH on it. GET returns defaultConfig while no record exists.
• llm.apiKey is accepted and discarded: never stored, never returned. The PUT body’s prompts stays all-or-nothing, as today.
• Remove s3 from configSchema, configUpdateSchema, publicConfig and ConfigDocument.
8.6 Parse route
LLM settings resolve per field: request body, then env (LLM_*), then stored settings (llm_base_url and llm_model only). The API key comes from the request or env, never from the hub. Instruction and prompts resolve request, then stored settings, then defaults. If the hub is unreachable, /parse still works with request and env values. The 200-with-rawResponse behavior for unparsable AI output stays.
8.7 Health
GET /health stays open and returns {status, checks:{hub, schema, ai}, timestamp}. hub is missing (not configured), unauthorized, error or connected, decided by an authenticated GET /api/entity-types (the hub’s own /health is open and says nothing about the token). schema is ok or missing_types with a missingTypes list of names. ai keeps its meaning, using the resolved server-side LLM settings. Never expose URLs, tokens or header values.
8.8 Remove
Move src/storage.ts to scripts/legacy/s3-store.ts. Delete src/request-settings.ts, src/r2/, r2-schema.ts, tests/r2/ and tests/config/request-settings.test.ts. Rewrite tests/config/config-route.test.ts for the hub. Remove S3 from config.ts, config-routes.ts, health.ts and parse-routes.ts. No S3 import may remain under src/; add a test that greps for it.
9. Frontend changes (apps/web, packages/shared)
1. plainlist-api.service.ts: remove the S3 types, setS3Config and the X-PlainList-S3-Config header. Add Authorization: Bearer <token> read from localStorage['plainlist-access-token']. That key is separate from the synced settings snapshot. A 401 sets sync status unauthorized, and the sync loop (timer, focus and online triggers) pauses until the token changes.
2. plainlist.service.ts: remove s3 from AppSettings, toLocalSettings, syncSettingsUpsert and applySnapshot. Add a one-time purge: when reading plainlist-angular-state and plainlist-backend-sync-state, delete settings.s3 and write the result back, so old S3 secrets leave the browser. Stop sending llm.apiKey in updateConfig.
3. Settings page: remove the S3 section. Add a “Backend access token” field (password input, saved to the separate key), a “Test connection” button (calls /health, then an authenticated /config) and a read-only sync status line: ok, unauthorized, unreachable or blocked.
4. Mass-deletion guard. An empty or wrong backend looks like “everything was deleted remotely”, and the three-way merge then drops the local copies. In reconcileSnapshots, if the merge would drop at least 5 local items and more than half of the items in the sync base, apply nothing, set status blocked and show a banner with two actions: “Accept deletions” (apply the merge) and “Keep my local data” (reset the sync base to the empty snapshot so the next sync pushes local items). Put the decision in a pure function in packages/shared/src/utils/sync-guard.ts (thresholds exported as constants) with unit tests.
5. packages/shared: remove configS3Schema and its uses. In healthResponseSchema replace checks.r2 with checks.hub and add checks.schema.
Nothing else changes in the app. The offline and queue behavior is not touched.
10. Migration and cutover
(redacted)
11. Tests and acceptance
Tooling: vitest in infra/backend, plus tests/helpers/fake-hub.ts, an in-memory hub implementing exactly the §4 calls with the hub’s semantics (32-hex ids, current-only queries, newest first, limit capped at 200, merge patch with null deletes, error envelopes, 401 without the token, X-Actor recorded). HubClient takes it through fetchImpl.
• Ids: dashed and hex round trip; malformed ids (../x, %2e%2e%2f, slashes, 31 or 33 characters, uppercase hex in the hub form) answer 404 with zero hub calls.
• Type confusion: the id of a person entity sent to every /tasks/:id, /grocery-lists/:id and /grocery-items/:id route (GET, PUT, DELETE) answers 404 and the fake hub records no write.
• Timestamps: 2026-10-17T12:00:00Z maps to createdAt …:00.000Z and updatedAt …:00.999Z.
• CRUD for the three resources; every response passes the shared zod schemas.
• Pagination: 450 tasks with limit=500 cost three hub queries; offset is honored.
• PUT: an unchanged payload causes zero PATCH calls; one changed field produces a PATCH body with only that field.
• DELETE archives; later reads and lists exclude the record; deleting a list archives its items.
• Items: unknown or archived list answers 400; moving an item validates the target list.
• Merge-items: quantities summed, notes joined, source archived; if the archive step fails the answer is 502 and the target keeps the merged values.
• Batch: partial-failure report; more than 25 entries answer 400.
• Search routes are reachable (route-order test).
• Config: GET returns defaults; first PUT creates; second PUT patches; a lost create race retries as PATCH; apiKey is never stored or returned; no s3 field anywhere.
• Parse: LLM settings resolve request, env, stored; the key never comes from the hub; a hub outage does not break /parse.
• Auth: missing, wrong and valid token; token not configured answers 503; OPTIONS answers 204; /health is open.
• CORS: an allowed origin is echoed; a disallowed one gets no allow header.
• Health: each hub and schema state.
• HubClient: timeout; retries on reads only; error mapping; extra headers sent; token absent from errors and logs.
• Bootstrap: creates missing types, is idempotent, reports drift without forcing.
• Shared: the guard function (below threshold, at threshold, the half rule, empty base).
• Contract suite (optional, not blocking in CI): the same CRUD tests run against a real throwaway hub when HUB_E2E_URL and HUB_E2E_TOKEN are set. It keeps the fake honest.
• Manual, at phone width: create a task on device A and see it on B; the chatbot’s query_entities on type task returns it; the chatbot sets state to completed and the app shows it after the next sync; deleting in the app leaves an archived record in the hub, not a forgotten one.
12. Sub-phases
• F0 Foundations: §2, config loading (§5), HubClient, ids and mappers, type definitions and hub:bootstrap, the fake hub. Done when: unit tests are green and bootstrap has run against a real hub, recorded in HUB_NOTES.md.
• F1 Backend swap (§8). Done when: all backend tests pass, a contract-suite run is recorded in the PR, lint and build are green, and no S3 code is left under src/.
• F2 Frontend and shared (§9). Done when: build is green, guard tests pass, and manually a wrong token shows unauthorized while an empty backend shows blocked and deletes nothing.
• F3 Migration and cutover (§10). Done when: a dry run against the real bucket works, an apply against a copy of the hub matches the counts, and the runbook is in the README.
13. Pitfalls
• The hub caps query limit at 200: page inside queryAll.
• PATCH takes the bare patch; null deletes a key; arrays are replaced whole.
• The hub’s /health is open: checking the token needs an authenticated call.
• embed: false means no embedding on write. Flipping it later does not index existing records.
• No unique_key on lists: otherwise a second device’s offline-created “Weekly groceries” loops on 409 and its items never sync.
• The hub validates date-time strictly: send ISO 8601 UTC with a trailing Z, and include a due_date round trip in the contract suite.
• Workers limit subrequests per invocation (the free plan has historically allowed 50; check the current number). Cascades and batches are sequential and capped. Self-hosted Node has no such limit.
• Hub order is newest first; S3 order was insertion order. The app sorts in the UI, but check tie-breaking on the todo and grocery pages.
• The app omits cleared optional fields from PUT payloads, so clearing a due date or a note never reaches the backend today. Out of scope; the hub would accept null to delete a key.
• exactOptionalPropertyTypes rejects undefined on optional properties in backend and shared.
• wrangler.toml must not contain the hub hostname or any token.
• The hub token has full access until scoped tokens exist. The type allowlist, the type check on every by-id call and id validation are the guard rails; keep them even after scoped tokens arrive.
14. Owner decisions (defaults chosen)
• D1 The facade stays; the browser never talks to the hub.
• D2 Type names task, grocery_list, grocery_item, plainlist_settings; the task field is state.
• D3 Ids created through the app are hub-assigned (the app already remaps ids on create). Client ids exist only in the migration, once the hub honors them (§3).
• D4 Delete means archive, with a cascade from list to items.
• D5 The LLM key never enters the hub; it comes from the request or the environment.
• D6 embed: false plus the owner’s capture exclusion (§3) now. When the hub gets per-type policy flags, change only src/hub/types.ts and re-run hub:bootstrap.
• D7 The hub’s single token for now; the guard rails in §13 apply.
• D8 Guard thresholds (5 items, more than half of the base) are constants in sync-guard.ts; tune later.
• D9 Backend access is a shared bearer token typed into Settings and kept in the browser. Cloudflare Access can replace it later.
