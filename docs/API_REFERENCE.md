# Hub API reference

The API uses the `{error:{code,message,...}}` error envelope. Entity types and schemas
remain owner-facing through REST; agent and MCP visibility is controlled separately by
each type's policy.

## Entity creation and listing

- `POST /api/entities` accepts `{type, data, uid?}`. A supplied `uid` must be 32 lowercase
  hexadecimal characters and is REST-only. A reused UID returns `409 conflict`.
- `GET /health` includes `features: ["client_uid"]`.
- `GET /api/entities` accepts a comma-separated `status` list of `current`, `archived`,
  and `superseded`; an unknown status returns `400 validation_error`. Omitting `status`
  preserves the existing all-status response.
- `GET /api/entity-types` retains each type's current-only `count` and adds status counts
  under `counts`.
- `POST /api/search` and `GET /api/search` accept `include_archived` (default `false`).

## Entity updates

`PATCH /api/entities/<uid>` accepts the existing JSON Merge Patch body. Optionally send
`X-Expected-Updated-At` with the entity timestamp returned by GET. A mismatch returns
`409 stale` and the current entity in `error.current`. Omitting the header preserves
existing client behavior.

## Entity type policy

`POST /api/entity-types` and `PATCH /api/entity-types/<name>` accept and return:

```json
{
  "policy": {
    "searchable": true,
    "capture_visible": true,
    "agent_visible": true,
    "auditable": true,
    "allow_extension": true
  }
}
```

`searchable` aliases the existing `embed` field. All values must be booleans; unknown
policy keys are rejected. `capture_visible`, `agent_visible`, `auditable`, and
`allow_extension` default to `true`.

- `capture_visible=false` hides the type from every Capture path. The
  `CAPTURE_EXCLUDE_TYPES` setting adds exclusions.
- `agent_visible=false` hides the type from tools and MCP; hidden type reads and writes
  return `not_found`. REST remains the owner's interface and is not filtered.
- `auditable=false` suppresses entity create, update, archive, restore, and supersede
  audit rows. Type/schema changes, forget, and type deletion remain audited.
- `searchable=false` is equivalent to `embed=false`.
- `allow_extension=false` locks the schema against REST, Capture, and tool/MCP extension
  requests while leaving the type's visibility policies unchanged. It can be changed by
  patching the type policy.

## Type extension tool

`extend_entity_type(name, add_properties, confirm=false)` is available through
`POST /api/tools/extend_entity_type` and is automatically reflected in the MCP manifest.
The default call validates and returns a proposed `diff` without writing. After explicit
owner approval, send `confirm=true`. Additions remain optional and additive, and use the
same validation and audit/history service as the REST extension route.
`TOOLS_ALLOW_EXTEND=false` makes calls return `forbidden`.
Types with `policy.allow_extension=false` also reject extension requests with `forbidden`;
the setting is available in REST and tool-based entity-type definitions.

## Entity type deletion

`POST /api/entity-types/<name>/remove-field` with `{"field": "x"}` is a dry run returning
`{applied:false, count, sample_ids (<=5)}`; add `"confirm": true` to apply. One transaction removes
the field from the schema (version +1), strips it from every entity of the type, copies removed
values to `schema_change_log` and rebuilds search text and embeddings. Required fields, the unique
key and types with `allow_extension=false` are refused. The same operation is the destructive
tool `remove_entity_type_field` (needs `PKB_TOOLS_ALLOW_DESTRUCTIVE`, and `PKB_TOOLS_ALLOW_EXTEND`).

`DELETE /api/entity-types/<name>?confirm=true` is REST/UI-only. `cascade=true` is
required when the type contains entities. Optional `convert_to_notes=true` requires
`cascade=true` and first converts each entity into a note. The UI offers a client-side
paged JSON export and requires the type name to be typed for confirmation.

Deletion returns `409 dependency` with `error.reasons`, `error.referenced_by`, and
per-status `error.counts` when blocked by a built-in type, schema references, a running
Capture job, or existing records without cascade. Cascade removes entity chunks,
vectors, search rows, and type history transactionally. No delete-type agent tool exists.

## Batch writes

`POST /api/batch` (same auth as every `/api/` route; actor from `X-Actor`) and the MCP tool `batch`:

```json
{"mode": "atomic", "dry_run": false, "ops": [
  {"op": "create_entity", "type": "person", "data": {"name": "Alice"}},
  {"op": "update_entity", "id": "<uid>", "patch": {"relationship": "friend"}},
  {"op": "supersede_entity", "id": "<uid>", "data": {"name": "Alice"}, "reason": "redo"},
  {"op": "archive_entity", "id": "<uid>", "reason": "obsolete"},
  {"op": "create_note", "content": "text", "tags": ["x"]}
]}
```

- `mode`: `atomic` (default; any failure rolls everything back, later ops are `skipped`) or `partial`
  (each op is isolated; good ops are kept). `dry_run=true` validates and saves nothing.
- Response `200`: `{ok, mode, dry_run, committed, results:[{index, op, ref?, status, id?, ok, result|error}], embeddings}`.
  `status` is `ok`, `error` or `skipped`; `ref` echoes the op's optional `ref` string; `id` is the
  created or changed entity/note id.
  A failed atomic batch is still `200` with `committed:false`.
- `400` malformed envelope; `413` more than `PKB_BATCH_MAX_OPS` (default 100) operations.
- Embeddings are computed once after commit; if that fails the data stays saved
  (`embeddings.failed > 0`) and a reindex repairs the vectors.
- The sensitive-data guard blocks per op (`secret_detected`, value never echoed).
- Agent actors (`agent:*`) cannot `create_note` here while `AGENT_NOTES_REQUIRE_REVIEW` is on
  (`review_required`); use `POST /api/notes`.
