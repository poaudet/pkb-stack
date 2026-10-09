# Addendum D — Owner (self), Gap-Filling, Schema Evolution

File: `docs/SELF_GAPS_SPEC.md`. Extends `docs/BUILD_SPEC.md` and
`docs/CAPTURE_SPEC.md` (their §0 rules, security rules and pitfalls
apply). Assumes Phases 0-4 and Capture C0-C2 are implemented; C3 (page)
and C4 (agent integration) may not be.

## 0. Preconditions (verify FIRST, report gaps in DECISIONS.md)
1. Capture modules and tables exist (`captures`, `capture_ops`,
   `capture_usage`); apply and undo work.
2. Where entity text is rendered for chunks (`text_template`) and where
   entity data is validated.
3. How `PATCH /api/entity-types/{name}` enforces additive changes today.
4. SQLite `fold()` is registered.
5. What apply does today when an included op `$ref`s an unticked op.
6. Whether `capture_ops.kind` has a CHECK constraint (new kind below).
If the repo differs from the spec, follow the repo and note it.

## 1. What ships
1. Owner record ("SELF"): one `person` with relationship "self".
2. Gap-filling: missing owner record, missing referenced records,
   similar-type guard.
3. Schema evolution: new OPTIONAL fields on existing types through the
   capture op `extend_entity_type`, with history and undo.
4. Catalog quality: field descriptions in the prompt; extra fields
   included in embedded text.
5. `get_self` tool; UI and agent notes for C3/C4.
Non-goals: removing/renaming/retyping fields; changing required or
unique_key; auto-apply; backfilling old records; merging duplicates;
detecting every mention in free text.

## 2. Gap kinds
```
SELF  first-person fact, no owner record -> offer to set the owner
REF   name in a reference field has no record -> propose minimal create
TYPE  no type fits -> create_entity_type (already exists)
ATTR  type fits, no field -> extend_entity_type (new)
```
The model proposes TYPE and ATTR. The server handles SELF and REF
deterministically. The owner approves everything.

## 3. Config additions (.env.example)
```
OWNER_NAME=                     # creates the SELF record at startup if none
CAPTURE_ALLOW_SCHEMA_CHANGES=true   # false: extend ops are dropped
CAPTURE_MAX_NEW_FIELDS=5        # per plan
CAPTURE_GAP_FILL=true           # propose missing referenced records
CAPTURE_GAP_MAX=8               # per plan
CAPTURE_GAP_DEFAULT_INCLUDE=true
CAPTURE_CATALOG_MAX_CHARS=5000  # was 3500
```

## 4. Data model
`ts` as in earlier specs.
```sql
-- guard with PRAGMA table_info; idempotent migration
ALTER TABLE entity_types
  ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS entity_type_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL REFERENCES entity_types(name) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  json_schema TEXT NOT NULL,
  change TEXT NOT NULL,        -- JSON: {"added":[...]} | {"removed":[...]}
  actor TEXT NOT NULL,
  capture_uid TEXT,
  at ts);
```
The migration inserts one version-1 history row per existing type.
`meta` key `self_uid`. `capture_ops.kind` gains `extend_entity_type`
(rebuild the table if a CHECK blocks it). New plan keys live in the
`captures.plan` JSON (no DDL).

## 5. Owner record (hub/self.py)
- Definition: zero or one CURRENT `person` whose `relationship` equals
  "self" (folded). `meta.self_uid` points at it. At startup validate the
  pointer; if several exist keep the oldest, log WARNING with ids, and
  expose `conflicts` in `GET /api/self`.
- `PUT /api/self {name, data?}`: no owner -> create the person with
  `relationship="self"`, set the pointer, audit `self_set`. Owner exists
  -> merge the given fields into it (relationship stays "self"). A name
  that collides with another current person -> 409 `conflict` naming it.
- `OWNER_NAME`: at startup, if no owner exists, behave like
  `PUT /api/self`. Idempotent; log INFO.
- Guards in the entity service (API, tools, capture all go through it):
  - A change that would produce two current owners -> 409 `conflict`.
  - Changing the owner's `relationship` away from "self", archiving, or
    forgetting the owner -> 409 `protected` unless `force=true`. Capture
    and agent tools never pass `force`; capture undo of an owner it
    created uses the internal path.
  - Superseding the owner is allowed; a hook updates the pointer.
- `is_first_person(text)`: folded regexes. EN `\b(i|i'm|i've|i'd|i'll|
  me|my|mine|myself)\b`; FR `\b(je|j'|moi|mon|ma|mes|m')\b`. Hints only;
  never gates a write.
- Tool `get_self()` (read-only): returns the owner entity or
  `{"exists": false}`. Description: "Use to read the owner's own facts
  (preferences, habits, birthday) before asking them."
- Convention: the owner is implicit in `people` lists; do not add the
  owner there unless the field is about the owner specifically.

## 6. Capture pipeline changes

### 6.1 Context (context.py)
- First line of EXISTING RECORDS is always the owner:
  `SELF | <id> | person | <name> | <=160 chars of interests/habits>` or
  `SELF: none yet`. It does not count toward CAPTURE_CANDIDATES.
- Catalog lines include field descriptions (<=60 chars) when present.
  Total budget CAPTURE_CATALOG_MAX_CHARS. When over budget, drop
  descriptions of builtin types first, never of fields marked
  `x-origin`.
- Seed upgrade (`ensure_builtin_types()`) may now ADD missing
  `description` metadata to existing seed properties; never overwrite an
  existing one. Required person descriptions:
  - relationship: "How they relate to the owner. 'self' only for the
    owner's own record."
  - habits: "Recurring routines or activities. NOT projects, work tasks
    or one-off events."
  - interests: "Topics, foods, hobbies or things they like."
  - notes: "Short free text. Do not use for facts that have a field."
  Write similarly concise descriptions for event, message and memory.

### 6.2 Prompt v2: Appendix A. Output contract: Appendix B.

### 6.3 Normalization additions (plan.py), in this order
1. Strip server-only keys from every LLM op: include, status, errors,
   warnings, diff, label, risk, requires, gap, source_gap.
2. SELF resolution: `target` equal to "SELF" (any case) or the owner uid
   -> owner uid. No owner exists -> op `invalid`, error `self_not_set`,
   one plan gap `{kind:"self", action:"set_owner"}`, one question "What
   name should I use for you?". A `create_entity person` whose name is
   in the stoplist {i, me, myself, moi, je, self, owner, user, mon, ma}
   or whose relationship folds to "self" -> convert to an update of the
   owner (warning `converted`), or `self_not_set` if none.
3. `extend_entity_type` validation (§7). Drop all when
   CAPTURE_ALLOW_SCHEMA_CHANGES=false (warning).
4. Reference resolution against the DB (not only the candidates): for
   every field with `x-ref` holding a plain string, look up CURRENT
   entities of the referenced type by folded label (label field =
   unique_key, else `name`, else `title`). One match -> rewrite to its
   id. Several -> keep the string, warning `ambiguous_ref` (<=3 labels),
   add a question. Strings equal to the owner's name/pronouns inside
   `people[]` are removed (warning `self_in_people`).
5. REF gap-fill (CAPTURE_GAP_FILL): for each remaining unresolved string
   S in an `x-ref` field targeting type T:
   - T exists, is not in CAPTURE_EXCLUDE_TYPES, and its required fields
     are a subset of {label field}.
   - S is 2-60 chars, <=4 words, and its folded form is not in the
     stoplist (EN/FR: friend(s), family, everyone, someone, somebody,
     people, colleagues, team, ami(s), famille, tout le monde,
     quelqu'un, gens, collegues, equipe).
   - One gap op per distinct (T, folded S), at most CAPTURE_GAP_MAX:
     `{id:"g1", op:"create_entity", type:T, ref:"g1",
       data:{<label field>: S}, risk:"auto_gap",
       gap:{kind:"ref", text:S, field:<field>, needed_by:[op ids]},
       include: CAPTURE_GAP_DEFAULT_INCLUDE}`.
     The referencing field becomes `{"$ref":"g1"}`. Label:
     "Add missing person — Sara".
   - Over the cap: leave plain strings and warn.
6. Similar-type guard for `create_entity_type`: normalize the name (fold,
   strip a trailing s/es/x, map spaces and hyphens to `_`). Same as an
   existing type -> drop the op (warning "type exists: <name>"); records
   of that name validate against the existing type. Otherwise compute
   the Jaccard overlap of property names with each existing type; >= 0.5
   -> keep, `risk:"similar_type"`, warning with the type and percent,
   `include:false`.
7. Dependencies: op B `requires` op A when B contains `{"$ref": A.ref}`,
   or B's type is created by A, or B sets a field that A (extend) adds.
   Store `requires:[...]` on B. Ops that require an unchecked op default
   to unchecked.
8. Existing guards (overwrites_text, unique_key conversion, empty
   updates).
9. Ordering: new types -> extends -> creates (dependencies first) ->
   updates -> supersedes -> note.
Each op carries `risk` in {none, new_type, schema_change, auto_gap,
similar_type, overwrites_text}. `plan.gaps[]` lists SELF-level gaps with
an `action` the UI can offer.

### 6.4 Apply additions (apply.py)
- Selection must be closed under `requires`. Otherwise 409 `dependency`
  with `{missing:[{op, requires}]}`. The server never auto-includes; the
  UI selects dependencies for the owner.
- Phase A validates and embeds against the POST-extension schema (hold
  extended schemas in memory) and renders text with §8.
- Phase B order: create types, extend types, entity ops. One
  transaction; any failure rolls back everything, including schema
  changes.
- `capture_ops` row for an extend: `entity_type`, `before` =
  `{"added":[names],"version_before":n,"version_after":n+1}`,
  `after_hash` = hash of the added property JSON.
- Self created by capture: sets the pointer inside the transaction.

### 6.5 Undo additions
- Reverse order. `update_entity` is restored first, then an extend is
  undone: remove the added properties only if the type's current
  `schema_version` equals `version_after` AND no entity of the type (any
  status) holds a non-null value for them. Otherwise keep and report
  `kept_in_use`. Removal writes a new version and a history row
  (`change:{"removed":[...],"undo_of":capture_uid}`).
- Undo of an owner created by the capture: archive it with the internal
  path and clear the pointer.

## 7. Schema evolution (hub/entity_types.py)
`extend(name, add_properties, *, actor, capture_uid=None, max_new=None,
dry_run=False)` is used by capture apply and by the API.
Rules, all enforced server-side with per-property errors:
1. The type exists. Capture additionally refuses types in
   CAPTURE_EXCLUDE_TYPES.
2. 1..max_new properties (capture: CAPTURE_MAX_NEW_FIELDS, at most 2
   extend ops per plan; manual API: 20). Total properties after <= 50.
3. Name matches `^[a-z][a-z0-9_]{0,40}$`; not reserved (id, uid, type,
   status, source, created_at, updated_at, superseded_by,
   status_reason); not already present under folding; not a sensitive
   name: reject any name containing password, passwd, passcode,
   passport, secret, token, apikey, api_key, ssn, credit, cardnum,
   card_num, cvv, iban, account_num, pin_code, pincode.
4. Allowed definitions only: `{"type":"string"}` with optional `format`
   date|date-time; `number`; `integer`; `boolean`; `array` whose
   `items.type` is string|number|integer|boolean. No enum, no objects,
   no nesting. Keywords kept: type, format, items, description.
5. `description` required, 3-120 chars.
6. Never touches `required`, `unique_key`, `text_template` or existing
   property definitions. New properties are never required.
7. Existing-data check: if any entity of the type already holds the key
   with a value that fails the new definition -> 409 `schema_conflict`
   with the count.
Effects, one transaction: add the properties with `x-origin`
("capture"|"manual") and `x-added` (date); `schema_version += 1`; insert
a history row; audit `type_extend` (names only). No existing entity is
rewritten and nothing is re-embedded.

## 8. Rendering and search
`render_entity_text(type, data)`: the template output, then, for each
schema property in schema order that has a non-empty value and is NOT a
placeholder in the template, append `; <name>: <value>` (lists joined by
", "). Skip object properties and `x-ref` properties. Cap extras at 800
chars. With no template, the default rendering already includes all
fields. Applies to new writes and reindex only. Result: a value stored in
a newly added field becomes findable by `search_knowledge`.

## 9. API
```
GET  /api/self        -> {self: entity|null, configured, conflicts?: [id]}
PUT  /api/self        {name, data?} -> 200 {self}
POST /api/entity-types/{name}/extend
                      {add_properties:{...}, confirm:true}
                      -> 200 {type, version, added:[...]}
GET  /api/entity-types/{name}/history -> versions with changes
```
New error codes: `protected` 409, `dependency` 409, `schema_conflict`
409. Op-level error code `self_not_set` appears in plans. Capture GET
plan adds per-op `risk`, `requires`, `gap` and `plan.gaps[]`. `/health`
adds `owner_configured`.

## 10. UI (build with C3; if C3 exists, extend it)
- Status/Settings: an "Owner" card: name input -> `PUT /api/self`;
  summary of the owner record with edit; a clear warning when missing.
- Capture page: a chip "Facts about you go to: <name>" (or a setup
  prompt when none); badges per op: `new field`, `missing record`,
  `similar type`, `new type`, `overwrites text`; dependency handling:
  ticking an op ticks its `requires`; unticking a required op unticks
  its dependents (with a note); an "Add field" card lists name, type and
  description (name and description editable, type from the allowed
  set); `plan.gaps` with action buttons (for `set_owner`: name input,
  then Regenerate).
- Entities page: type detail shows schema version and history; "Add
  field" button -> `POST .../extend`.
- Same rules as before: no innerHTML, no inline script/style.

## 11. Agent integration (build with C4)
- Add `get_self` to the registry; include it in the `slim` profile.
- `capture_text` description: add "Facts about the owner (I, my, je,
  mon) are handled; pass the words verbatim."
- SKILL.md: read owner preferences with `get_self`; for anything to
  remember call `capture_text`; do not edit the schema yourself.

## 12. Security & privacy
- The model cannot remove or retype fields, cannot touch excluded types,
  and every schema change needs the owner's tick. Property names are
  regex-restricted and blocklisted for secrets.
- Owner facts can be sensitive (health, finances). They go to the
  capture LLM only as <=160-char owner one-liners and the note itself;
  show the host and local/remote chip as before.
- Audit every self/schema action with ids and names only, never values.
- Forgetting the owner needs `force`; capture and tools cannot pass it.
- Descriptions and names render with `textContent` only.

## 13. Tests & acceptance
Same stub LLM harness as capture. Golden cases:
- H1 owner exists, "I like kiwis a lot" -> one valid `update_entity` on
  the owner adding "kiwis" to `interests` (union); no note, no new type.
- H2 no owner, OWNER_NAME unset, same text -> op `invalid`
  (`self_not_set`), `plan.gaps` has `set_owner`, one question. After
  `PUT /api/self` and regenerate -> H1.
- H3 (Appendix C) "My favorite color is blue": `extend_entity_type`
  (`risk:schema_change`, unchecked) + owner update that `requires` it.
  Apply {o2} -> 409 `dependency`. Apply {o1,o2} -> field exists,
  `schema_version` +1, history row, owner updated, text findable via
  search. Undo -> value removed, property removed.
- H4 "Went to Portland with Sara last Saturday", no Sara -> event op +
  gap op `create person Sara` (included, `auto_gap`), event.people
  holds `{"$ref"}`. "friends" alone creates no gap.
- H5 stub proposes `create_entity_type pet` while `animal` has
  overlapping fields -> warning, unchecked. Proposing `animals` while
  `animal` exists -> dropped.
- H6 extend validation: reserved name, existing name, sensitive name,
  retype attempt, 6 properties (truncated + warning), enum/object
  definition, missing description, existing-data conflict.
- H7 rendering: extras appended in order, objects and `x-ref` skipped,
  800-char cap; a value in a new field is returned by hybrid search.
- Self guards: second owner rejected, archive/forget/relationship change
  need `force`, supersede moves the pointer, startup repair with
  duplicates, `OWNER_NAME` idempotent.
- First-person detector: EN and FR cases (hint only).
- Server-only keys in LLM output are stripped (assert a stub response
  containing `include:true` or `requires` is ignored).
- Prompt budget: whole request <= 24k chars; catalog <= configured max.
- Existing Phase 1-4 and C0-C2 tests stay green.

## 14. Sub-phases (one PR each; stop and report after each)
- D0 Owner + catalog quality: self.py, `PUT/GET /api/self`, OWNER_NAME,
  guards, `get_self`, seed descriptions, renderer extras, catalog
  descriptions/budget, SELF context line, prompt rules 10-11. DoD: H1,
  H2, H7, self-guard tests.
- D1 Gap-filling: DB ref resolution, REF gaps, stoplist, dependency graph
  and 409, similar-type guard. DoD: H4, H5, dependency tests.
- D2 Schema evolution: migration, `extend()`, endpoints, op kind, prompt
  rule 12, apply and undo. DoD: H3, H6.
- D3 UI and agent: with C3/C4 (see §10-11). DoD: manual script on phone
  width: set owner, capture "I like kiwis", capture "my favorite color
  is blue" (add field), undo.

## 15. Pitfalls (do not repeat)
- Never trust server-only keys or ids from LLM output.
- Do not use the RFC 7386 merge for capture updates (existing rule).
- SQLite cannot drop columns easily: the migration only adds.
- Compare `relationship` folded; "Self" and "SELF" are the same.
- The owner is implicit in `people[]`.
- Do not re-embed existing entities when a type is extended.
- First-person detection is a hint, not a gate.
- Dependencies are never auto-included by the server.
- Extras rendering must skip objects and `x-ref` ids (noise, privacy).

## 16. Owner decisions (defaults chosen)
- D1 Schema changes are unchecked by default and need a tick.
- D2 Missing-record gap ops are checked by default (low risk, undoable);
  set CAPTURE_GAP_DEFAULT_INCLUDE=false to change.
- D3 Set `OWNER_NAME` or call `PUT /api/self` once after deploy.
- D4 The owner's facts live in the `person` type; a separate profile type
  would be a later migration.

## Appendix A: prompt v2 changes (prompt.py)
Insert after the EXISTING RECORDS block:
```
OWNER (SELF). First-person facts ("I", "my", "me", "je", "j'ai", "mon",
"ma", "mes") are about this record: {self_line_or "none yet"}.
If the owner record is none, still write "target":"SELF" in ops about
the owner; the system will ask for the owner's name.
```
Op kinds block: add
```
{"id":"o5","op":"extend_entity_type","type":T,
 "add_properties":{"field_name":{"type":"string|number|integer|boolean|
 array","description":str}},"why":str}
```
Replace rule 7 and add rules 10-13:
```
7. Propose a new type only if no existing type fits AND similar records
   will likely recur. At most one. Prefer an existing type that is close.
10. Facts about the owner go to SELF (update_entity, target "SELF"). Do
    not add the owner to "people" lists.
11. Use each field only for what its description says. If the fact does
    not match any field, do not stretch an unrelated one.
12. If a fact belongs to an existing type but no field can hold it AND
    the same attribute would apply to other records of that type,
    propose ONE extend_entity_type with at most 3 optional fields
    (snake_case names, each with a short description), then set the
    value in the same plan. Otherwise use notes or interests, or
    keep_note.
13. For people or records mentioned but absent from EXISTING RECORDS you
    may create them or simply write their names in reference fields; the
    system will offer to create them. Never use pronouns as names.
```

## Appendix B: plan schema v2 (diff against CAPTURE_SPEC Appendix B)
```json
"op":{"enum":["create_entity","update_entity","supersede_entity",
              "create_entity_type","extend_entity_type"]},
"add_properties":{"type":"object","maxProperties":5,
 "additionalProperties":{"type":"object","required":["type","description"],
  "properties":{
   "type":{"enum":["string","number","integer","boolean","array"]},
   "format":{"enum":["date","date-time"]},
   "items":{"type":"object","properties":{
     "type":{"enum":["string","number","integer","boolean"]}}},
   "description":{"type":"string","minLength":3,"maxLength":120}}}}
```
`target` may be the literal "SELF". Per-op requirements (type for
extend, add_properties non-empty) are checked in code.

## Appendix C: golden H3
Catalog: person(name!*, relationship, interests[], habits[], notes).
Owner exists (id `s1`). Input (as_of 2026-10-09): "My favorite color is
blue." Stub response:
```json
{"summary":"Favorite color: blue",
 "ops":[
  {"id":"o1","op":"extend_entity_type","type":"person",
   "add_properties":{"favorite_color":{"type":"string",
     "description":"Favorite color"}},"why":"no field holds it"},
  {"id":"o2","op":"update_entity","target":"SELF",
   "patch":{"favorite_color":"blue"},"why":"owner preference"}],
 "keep_note":false,"note":null,"questions":[]}
```
Expected: o1 `risk:schema_change`, include false; o2 `requires:["o1"]`,
include false; apply {o2} -> 409 `dependency`; apply {o1,o2} -> person
schema has `favorite_color` (version +1, `x-origin: capture`), owner has
`favorite_color: "blue"`; undo restores the owner then removes the
property.