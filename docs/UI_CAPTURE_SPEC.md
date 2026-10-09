# Addendum E — Web App Wiring: Capture Page, Owner, Schema UI (C3 + D3)

File: `docs/UI_CAPTURE_SPEC.md`. Extends `docs/BUILD_SPEC.md`,
`docs/CAPTURE_SPEC.md` and `docs/SELF_GAPS_SPEC.md`. Where this doc
conflicts with CAPTURE_SPEC §8 or SELF_GAPS_SPEC §10, THIS doc wins.
Assumes Phases 0-4, Capture C0-C2 and D0-D2 are implemented. Their §0
rules, security rules and pitfalls apply.

## 0. Preconditions (verify FIRST; write findings to docs/UI_NOTES.md)
1. Inventory the existing UI: files in `hub/static/`, how tabs and routes
   are registered, DOM helpers, the API client and token handling (401
   prompt), CSS variables/classes, how the Review and Entities tabs
   fetch and render.
2. Run the full test suite and `docker compose up`. Fix nothing; report.
3. With curl, record the REAL response shapes of `POST /api/capture`,
   `GET /api/capture/{id}`, apply, undo, regenerate, `GET/PUT /api/self`,
   `GET /api/entity-types`, `POST .../extend`, `GET .../history`.
   Known so far: the GET body is `{"capture": {...}}`; ids are hex
   strings; ops carry both `kind` and `op`; `plan` holds `summary`,
   `ops`, `questions`, `warnings`, `keep_note`, `note`; each op has
   `id, op, type, label, why, status, include, errors, warnings, diff,
   target?, data?, patch?` and, after D1/D2, `risk, requires, gap`.
4. Check which §2 API gaps already exist.
5. Check whether `scripts/stub_llm.py` exists.
Record deviations in DECISIONS.md. If the repo differs from this spec,
follow the repo and say so.

## 1. What ships
- **Capture page**: composer, plan review with editable ops, dependency-
  aware selection, apply, results, undo, history, deep links.
- **Owner UI**: owner card, setup flow, the `set_owner` gap action.
- **Schema UI**: type details, version history, "Add field", protected-
  owner confirmation.
- Integrations: Add-tab shortcut, Status cards, nav badge.
- Playwright end-to-end tests at phone size.
Non-goals: audio capture (use the keyboard's dictation), editing type
schemas beyond adding fields, a French UI (strings isolated for later),
agent integration (C4).

## 2. API gaps (implement ONLY what is missing; tests; BUILD_SPEC rules)
1. `GET /api/capture/config` -> `{enabled, model, host, local, max_chars,
   calls_today, daily_limit, resets_at}`.
2. `GET /api/capture?state=&source=&limit=&offset=` ->
   `{captures:[{id,state,source,created_at,summary,model,op_count,
   error?}], total}`. `summary` = plan.summary, else the first 80 chars
   of the raw text.
3. Apply accepts per-op overrides: `include`, `data` (create/supersede),
   `patch` (update), `add_properties` (extend; re-validated by
   SELF_GAPS_SPEC §7), plus top-level `keep_note` and `allow_new_types`.
   Everything is re-validated server-side. If the implementation needs
   another confirmation flag for extend ops, the UI sends it after its
   own confirm dialog.
4. `POST /api/capture/{id}/regenerate {answers?, hint?}`: `answers` maps
   question text -> answer text; state ready|failed -> queued; counts
   toward the daily limit; otherwise 409 `capture_state`.
5. `POST /api/entities/labels {ids:[<=100]}` -> `{labels:{<id>:{type,
   label}}}` using the label rule (unique_key, else name, else title);
   includes archived; unknown ids omitted.
6. `GET /api/entities/{id}` and `GET /api/notes/{id}` return the item
   whatever its status (result links must work after archive).
7. `GET /api/entity-types` returns per type: `json_schema,
   schema_version, builtin, unique_key, description`.
8. Protected owner: archive and forget accept `force=true` (query or body,
   follow the implementation); otherwise 409 `protected`.
9. `POST /api/capture/{id}/validate` (same body as apply, NO writes, not
   counted toward the limit, 409 `capture_state` unless ready) ->
   `{ops:[{id,status,errors,warnings,diff,requires,label,risk}],
   selection:{ok, missing:[{op,requires}]}}`. Used for live feedback
   after edits.

## 3. UI architecture
- Static files; no framework, build step or CDN. ES modules via
  `<script type="module">` (CSP `script-src 'self'` allows them). Keep
  `app.js` as the shell if it is monolithic and add modules:
```
static/capture/api.js        all capture/self/type calls + shape adapters
static/capture/state.js      selection, overrides, dependency closure (pure)
static/capture/page.js       composer, polling, plan, results, history
static/capture/opcard.js     op card rendering
static/capture/schemaform.js JSON Schema -> field descriptors + DOM
static/capture/diff.js
static/owner.js  static/types.js  static/dialog.js  static/labels.js
static/strings.js            ALL user-visible text
```
- Rules: no `innerHTML` with data (use `createElement`/`textContent`; a
  helper `h(tag, attrs, ...children)`); no inline script/style/handlers;
  only internal `#` links; every server-supplied string goes through
  `textContent`; style with classes; use `<progress>` for progress;
  `<dialog>` + `showModal()` for dialogs.
- All shape differences are normalized in `api.js` (unwrap `{capture}`,
  prefer `op` over `kind`, treat ids as opaque strings). Nothing else
  touches raw responses.
- Routes: `#capture`, `#capture/<id>`, `#entity/<id>`, `#note/<id>`,
  `#types/<name>`, `?tab=capture`. The last tab is remembered in
  localStorage; first visit lands on Capture.
- Polling: a `setTimeout` chain every 2 s, paused while the page is
  hidden, cancelled on route change, stops after 5 min; after 60 s show
  "still working; slow models can take a while".
- Requests use `AbortController`; a 401 triggers the existing token
  prompt and one retry.

## 4. Capture page

### 4.1 Layout
Mobile first, single column. At >= 900 px two columns: composer + history
left, plan right. Mobile has a sticky bottom action bar (use `100dvh`
units; respect safe-area insets).
```
┌ Capture ──────────────────────────────┐
│ AI: <host> · remote · 12/60 today      │
│ Facts about you go to: <owner name>    │
│ [ textarea ............ 812/8000 ]     │
│ ▸ Add context   as of [2026-10-09]     │
│ ☐ Save as note without AI              │
│ [ Analyze ]                            │
├ Plan ─────────────────────────────────┤
│ summary · model · tokens               │
│ ⚠ warnings   ? questions   gaps card   │
│ ▢ [Create] person — Sara  [Missing rec]│
│ ▢ [Update] person — <owner> (diff)     │
│ ▢ [Add field] person.favorite_color    │
│ ☑ Also keep my text as a note          │
│ ───── sticky bar ─────                 │
│ 2 of 3 selected  [Apply]  [Reject] ⋯   │
├ Recent captures ──────────────────────┤
└───────────────────────────────────────┘
```

### 4.2 Composer
- Textarea: min 6 rows; no autofocus on touch devices; `maxlength` =
  config `max_chars` with a live counter; `autocapitalize="sentences"`,
  `spellcheck` on, `enterkeyhint="send"`.
- "Add context" (collapsed): hint input.
- As-of date input (default today, local). Always sent as YYYY-MM-DD.
- Checkbox "Save as note without AI" (`skip_ai`), remembered per device.
- Privacy chip from config: host + `local|remote` + usage `n/limit` and
  reset time on hover/tap. If `enabled=false` or the API answers
  `llm_not_configured`: hide Analyze, keep only "Save as note".
- Ctrl/Cmd+Enter submits. The button is disabled while empty or in
  flight (no double submits).
- Draft: localStorage `pkb.capture.draft` `{text, hint}`, saved debounced
  (500 ms), restored on load, cleared ONLY after a 2xx create.
- Submit: `POST /api/capture {text, hint?, as_of, skip_ai?}`. 201 ->
  toast with link `#note/<id>`. 202 -> route to `#capture/<id>` and poll.
- Client pre-check: over `max_chars` -> inline "Too long for AI
  capture" with a "Save as note" button.

### 4.3 States
| Server state | View |
|---|---|
| queued, processing | `<progress>` + elapsed time + "Stop waiting" (goes to history; the capture continues) |
| ready | plan view (§4.4) |
| failed | error text + [Regenerate] [Save as note instead] [Edit text] |
| applied | results (§4.7) with Undo |
| rejected, undone | read-only summary + "Start a new capture" |
| anything else | show the raw state label |
"Edit text" returns to the composer with the raw text when the server
still has it (it is NULLed after the retention window; then hide it).

### 4.4 Plan view
Order: header (summary; model chip; tokens `prompt/completion`) ->
warnings (plan.warnings and gap messages) -> questions -> gaps card ->
op list -> "Also keep my text as a note" (initial = plan.keep_note; shows
plan.note title/tags read-only; the saved content is always the raw
text) -> sticky bar.
- Questions: label + input each. "Regenerate with answers" is enabled with
  >= 1 answer; sends `answers` mapping question text -> answer;
  accumulates answers across regenerations.
- Gaps card: one row per `plan.gaps[]`; `set_owner` renders the inline
  owner form of §5.
- Sticky bar: "N of M selected", [Apply selected] (disabled at 0 or while
  any selected op is invalid), [Reject], and a menu: Regenerate, Save as
  note instead.

### 4.5 Op cards
- Title = `label`. Badge by kind: create "Create", update "Update",
  supersede "Replace", create_entity_type "New type", extend_entity_type
  "Add field". Risk/status badges (always text, never colour alone):
  `schema_change` "New field", `new_type` "New type", `auto_gap` "Missing
  record", `similar_type` "Similar type exists", `overwrites_text`
  "Overwrites text", `converted` "Converted", `invalid` "Needs fixing",
  `edited` "Edited".
- Muted `why` line. Errors in red and warnings in amber, each with an
  icon and text.
- "Needs: <label of required op>" for each `requires`.
- Body:
  - create / supersede: editable form for `data` (§4.6 mapping). Show the
    fields that have values plus a "More fields" expander listing the
    rest of the schema.
  - update: old -> new diff table; text fields show old and new, list
    fields show chips with additions highlighted; "Edit changes"
    expander opens the form for `patch` fields only.
  - create_entity_type: read-only schema summary (field table).
  - extend_entity_type: list of `name — type — description`; name and
    description editable; type read-only. Sent as `add_properties`.
- Checkbox with `aria-label` = op label. Invalid ops cannot be ticked
  until a validate call reports them valid.

### 4.6 Field editing (schemaform.js)
Descriptors come from the type's JSON Schema (`GET /api/entity-types`,
cached; refreshed after any extend). Mapping:
string -> text input; names `notes|description|text|content` -> textarea;
`format:date` -> `<input type=date>`; `format:date-time` -> text input
with an ISO placeholder; `enum` -> select; boolean -> checkbox;
number/integer -> number input (`step` for integer); array of scalars ->
comma-separated input (trim, drop empties); `x-ref` fields -> read-only
chips showing resolved labels (removable); anything else -> JSON textarea
with `JSON.parse` validation. Required fields show `*`. Field
`description` is the help text. Plain-name references to ops of the same
plan show that op's label. An edited value is stored as an override only
when it differs from the original. After edits, call `/validate`
(debounced 600 ms) and refresh errors, diffs and `requires`.
Labels for ids are resolved in batch via `POST /api/entities/labels` and
cached (`labels.js`).

### 4.7 Selection, dependencies, apply, results, undo
- `state.js` (pure, unit-testable): `closureUp(selected, ops)` adds all
  transitive `requires`; `closureDown(selected, ops, removed)` removes
  every op that transitively requires a removed one. Ticking an op
  ticks its requirements (toast "also selected: ..."); unticking removes
  dependents (toast "also unselected: ..."). A gap op whose `needed_by`
  ops are all unticked becomes unticked too. Ticking an op that requires
  an invalid op is refused with a message.
- Apply flow:
  1. Build the body from the selection and overrides; `keep_note` from
     the checkbox; `allow_new_types=true` only if a new-type op is
     selected.
  2. Confirm dialogs: any selected `schema_change` or `new_type` op ->
     dialog listing `type.field (kind) — description`, buttons "Add
     field(s)" / "Cancel". Any `overwrites_text` -> a confirm naming the
     field.
  3. `POST /api/capture/{id}/apply`; disable the bar while in flight.
  4. 409 `dependency` -> add `missing` ops to the selection, toast, do
     not resend. Other 4xx -> show the message on the op named by the
     error (`op_id`) and scroll to it.
- Results view: `applied[]` rows with an icon by kind, label and link:
  entity `#entity/<id>`, note `#note/<id>`, type change `#types/<type>`.
  `skipped[]` with reasons. Buttons: [Undo], [New capture].
- Undo: confirm, `POST /undo`, then show the report: undone items and
  `kept_modified` / `kept_in_use` items with a one-line explanation each.

### 4.8 History
List: relative time, state badge, source badge (`ui`, `api`,
`agent:<name>`), summary, model. Filter chips: All, Needs review
(ready), Applied. 20 per page. Row -> `#capture/<id>`. `ready` captures
from agents get an accent "Needs your review". The nav tab shows a badge
with the count of `ready` captures (refresh on load, on route change,
and every 60 s while visible).

### 4.9 Errors (toasts/banners, no raw JSON)
| API | UI |
|---|---|
| 400 `too_long` | "Too long for AI capture (max N)." [Save as note] |
| 409 `secret_detected` | "Looks like it contains a <kind>. Nothing was sent." [Edit text] [Send anyway] (confirm, then resend with `allow_secrets:true`) |
| 409 `duplicate` | "You captured this on <date>." [Open it] [Analyze anyway] (`allow_duplicate:true`) |
| 429 `rate_limited` | "Daily AI limit reached. Resets at <local time>." [Save as note] |
| 409 `llm_not_configured` | notice; Analyze hidden |
| 409 `capture_state` | refresh the capture |
| network/timeout | "Couldn't reach the hub." [Retry]; the draft is preserved |
| 5xx | generic message; no response body shown |

## 5. Owner and gaps UI (owner.js)
- On load: `GET /api/self`, cached. Capture page chip: configured ->
  "Facts about you go to: <name>"; missing -> warning chip with "Set
  up" opening the owner dialog.
- Owner form: name (required) plus fields built from the `person` schema
  (birthday, interests, habits, notes and any added fields); hide
  `relationship` and `contact`. Save -> `PUT /api/self {name, data}`,
  toast, refresh the cache. If `conflicts` is present, show a warning
  with links to those entities.
- Status page shows the same form inline as the "Owner" card.
- `set_owner` gap row: name input + [Save and re-analyze] -> `PUT
  /api/self`, then `regenerate`.
- Ops with `self_not_set` show "Needs an owner record" and a button that
  scrolls to the gaps card.

## 6. Schema UI (types.js, Entities tab)
- Type selector shows a `v<schema_version>` badge. A collapsible "Type
  details" lists fields: name, type, `!` required, `*` unique,
  description, origin badge (`capture` / `manual`).
- "Add field" dialog: name (hint: lowercase letters, digits, `_`), type
  (Text, Number, Whole number, Yes/No, Date, Date & time, List of text),
  description. Submit `POST /api/entity-types/{name}/extend
  {add_properties:{<name>:{type,format?,items?,description}},
  confirm:true}`. Map per-property server errors to field messages.
  Success: refresh caches, close, toast.
- "History" panel from `GET .../history`: version, date, actor, change
  summary ("added favorite_color"), link to the capture when
  `capture_uid` is set.
- Protected owner: archive/forget on the owner record -> 409
  `protected` -> dialog "This is your owner record. Capture can't file
  facts about you until you set a new one." -> retry with `force=true`
  only after explicit confirmation. No automated path ever forces.
- Read-only dialogs for `#entity/<id>` and `#note/<id>` (fields from the
  schema; text via `textContent`).

## 7. Integration with other tabs
- Add tab: button "Analyze into records →" copies the textarea into the
  capture draft and routes to `#capture`.
- Search results of kind entity link to `#entity/<id>`.
- Status page: capture card (model, host, local/remote, usage, reset
  time, "Skip AI by default" toggle) and the owner card.
- Nav order: Capture, Search, Add, Review, Browse, Entities, Status.

## 8. Accessibility and mobile QA
- `aria-live="polite"` region for state changes ("Plan ready", "Applied
  3 changes"); move focus to the plan heading when the plan appears.
- Labels on every input; checkboxes have accessible names; dialogs trap
  focus and close on Escape; badges carry text.
- Tap targets >= 44 px; the sticky bar never covers the last card (bottom
  padding = bar height); works at 360 px width; dark mode via
  `prefers-color-scheme`; no hover-only controls.
- Keyboard: Ctrl/Cmd+Enter submits; Tab order follows visual order.

## 9. Security
- Everything in BUILD_SPEC §12 and CAPTURE_SPEC §10 applies.
- LLM-supplied text (labels, warnings, values) is untrusted: always
  `textContent`; links are internal `#` routes built from ids that match
  `^[a-f0-9]{8,64}$`.
- Never log or persist capture text outside the draft key; clear the
  draft after a successful create.
- The token is stored the same way as the existing UI does it; do not
  add new storage.

## 10. Tests
Dev dependency `pytest-playwright` (Chromium). Marker `@pytest.mark.e2e`,
excluded from the default run; a separate CI job runs `playwright install
--with-deps chromium` then the e2e tests. A fixture starts the Flask app
on a free port with `EMBED_BACKEND=fake`, a temp DATA_DIR and the stub LLM.
Extend `scripts/stub_llm.py` to serve scenarios by matching a substring of
the note (fixtures in `tests/fixtures/capture/*.json`; Appendix B).
Run each e2e at 390x844 and 1280x800.
- E1 G1 text -> plan with 3 ops; untick one; edit a field; apply;
  results list with working links; Undo -> items archived, history shows
  `undone`.
- E2 owner flow: no owner -> H2 plan shows the `set_owner` gap; enter a
  name; plan regenerates into the H1 update with a diff table.
- E3 H3: tick the owner update -> the field op auto-ticks; untick the
  field op -> the update unticks with a toast; apply both -> Entities
  type details show the new field and `v+1`; Undo removes the field.
- E4 failure: the stub returns invalid output twice -> `failed` state ->
  "Save as note instead" creates a note.
- E5 gates: secret text -> message and "Send anyway" works after
  confirm; too long; rate limit message with reset time; duplicate.
- E6 draft: type, reload -> restored; a failed network call keeps the
  text; cleared after success.
- E7 XSS: a plan whose labels/values contain `<img src=x onerror=...>`
  renders as text and `window.__xss` stays undefined.
- E8 auth: with `API_TOKEN` set, the first call prompts for the token and
  then succeeds.
- E9 protected: archiving the owner shows the confirm dialog; Cancel
  leaves it unchanged; Confirm archives.
- E10 mobile: at 390 px the sticky bar is visible and does not overlap the
  last card; primary buttons are >= 44 px tall.
- Unit: `state.js` closures; `schemaform.js` descriptors; the `innerHTML`
  grep extended to every new file.
- Server: tests for each §2 gap that had to be built.

## 11. Sub-phases (one PR each; stop and report after each)
- E0 Foundations: §0 inventory in UI_NOTES.md, §2 API gaps, `api.js`
  adapter, stub scenarios, Playwright harness with one smoke test. DoD:
  curl checks documented; tests green.
- E1 Capture page core: composer, config chip, draft, create + poll,
  states, read-only plan view, history + deep links, error mapping, nav
  entry and badge. DoD: E4, E5, E6, E8 pass and the plan renders.
- E2 Editing, selection, apply, undo: schema form, diff, dependencies,
  validate, confirm dialogs, results, undo, regenerate with answers.
  DoD: E1, E3, E7 pass.
- E3 Owner and schema UI + integrations: owner card/dialog, set_owner
  flow, type details/history/add-field, protected dialog, Add-tab
  button, Status cards, a11y pass. DoD: E2, E9, E10 pass plus a manual
  phone check (set owner; capture "I like kiwis"; capture "my favorite
  color is blue" and add the field; undo).

## 12. Pitfalls (do not repeat)
- Ids are opaque hex strings, never integers. The response is wrapped in
  `capture`; ops have `op` and `kind`. Normalize in `api.js` only.
- Never hard-code entity types or fields: derive from schemas, and refresh
  after an extend.
- Keep `YYYY-MM-DD` as strings; never round-trip them through `Date` (UTC
  shifts).
- Do not send unchanged overrides; do not send server-only keys.
- Do not auto-include dependencies on the server; the UI handles it.
- Do not clear the draft on error. Do not double-submit.
- Stop polling on route change and when the page is hidden.
- `<dialog>`: always handle Escape/cancel so state is not left half-open.
- Sticky bars hide content unless the page has matching bottom padding.
- Do not display `$ref` aliases or raw ids to the user: show labels.

## 13. Owner decisions (defaults chosen)
- D1 Capture is the first tab and the landing page on first visit.
- D2 UI text is English; strings are isolated for a later French pass.
- D3 Missing-record gap ops follow the server's default tick.
- D4 The e2e CI job starts non-blocking and becomes required once stable.

## Appendix A: stub scenarios (tests/fixtures/capture/)
Each file: `{match: "<substring of the note>", response: {...plan}}`.
- `g1_pmt.json` (CAPTURE_SPEC Appendix C).
- `h1_kiwis.json`: match "kiwis" -> update on target "SELF" adding
  "kiwis" to interests.
- `h2_no_owner.json`: same text; the harness runs it with no owner.
- `h3_color.json` (SELF_GAPS_SPEC Appendix C).
- `h4_sara.json`: "Went to Portland with Sara" -> event op with
  `people:["Sara"]` (the server adds the gap op).
- `fail_twice.json`: returns non-JSON on two consecutive calls.
- `xss.json`: labels and values containing HTML/script payloads.