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
    "auditable": true
  }
}
```

`searchable` aliases the existing `embed` field. All values must be booleans; unknown
policy keys are rejected. New type flags default to `true`.

- `capture_visible=false` hides the type from every Capture path. The
  `CAPTURE_EXCLUDE_TYPES` setting adds exclusions.
- `agent_visible=false` hides the type from tools and MCP; hidden type reads and writes
  return `not_found`. REST remains the owner's interface and is not filtered.
- `auditable=false` suppresses entity create, update, archive, restore, and supersede
  audit rows. Type/schema changes, forget, and type deletion remain audited.
- `searchable=false` is equivalent to `embed=false`.

## Type extension tool

`extend_entity_type(name, add_properties, confirm=false)` is available through
`POST /api/tools/extend_entity_type` and is automatically reflected in the MCP manifest.
The default call validates and returns a proposed `diff` without writing. After explicit
owner approval, send `confirm=true`. Additions remain optional and additive, and use the
same validation and audit/history service as the REST extension route.
`TOOLS_ALLOW_EXTEND=false` makes calls return `forbidden`.

## Entity type deletion

`DELETE /api/entity-types/<name>?confirm=true` is REST/UI-only. `cascade=true` is
required when the type contains entities. Optional `convert_to_notes=true` requires
`cascade=true` and first converts each entity into a note. The UI offers a client-side
paged JSON export and requires the type name to be typed for confirmation.

Deletion returns `409 dependency` with `error.reasons`, `error.referenced_by`, and
per-status `error.counts` when blocked by a built-in type, schema references, a running
Capture job, or existing records without cascade. Cascade removes entity chunks,
vectors, search rows, and type history transactionally. No delete-type agent tool exists.
