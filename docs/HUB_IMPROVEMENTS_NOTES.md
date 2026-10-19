# Hub improvements orientation

Baseline: `pytest -q` passed (80 passed, 13 deselected); `ruff check .` passed.

| Assumption | Finding |
|---|---|
| Entity statuses and query defaults | Confirmed: `current`, `superseded`, `archived`; entity query defaults to current, and list accepts a status. |
| Type schema/history markers | Confirmed: schema version/history exist; extension metadata uses `x-origin` and `x-added`. |
| Capture exclusions | Differs: catalog honored `CAPTURE_EXCLUDE_TYPES`, while candidates, normalization, and apply did not enforce it. |
| REST type extension | Confirmed: route and shared service exist; no tool wrapped it. |
| Type deletion route | Differs: a limited DELETE route already existed; it lacked confirmation, dependency checks, and cascade behavior. |
| Client UID | Confirmed: service accepted a UID; REST create did not pass it. |
| UI/CSP | Confirmed: static JavaScript renders API data under an existing CSP. |
| Tool/MCP registry | Confirmed: tools use one registry and MCP loads the REST manifest; destructive tools are gated. |
| Entity audit | Confirmed: regular entity writes previously audited every time. |
| Python date-time support | Confirmed: Python 3.11 accepts ISO date-times with `Z` and fractional seconds. |

Phase 5 centralizes capture, agent, and audit policy while preserving the phase 1
environment override. The entity-type deletion implementation reuses the existing
SQLite vector store and note indexing paths.
