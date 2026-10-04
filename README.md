# Personal Knowledge Hub

Self-hosted, single-user notes with hybrid (keyword + semantic) search. Spec: `docs/BUILD_SPEC.md`.
Status: phases 0-4.

## Quick start
```
cp .env.example .env
docker compose up -d --build
open http://127.0.0.1:5000
```
Dev: `pip install -r requirements.txt -r requirements-dev.txt && EMBED_BACKEND=fake pytest -q`.
End-to-end: `scripts/smoke.sh`.

## Environment
All variables are optional; see `.env.example`. Set `API_TOKEN` before binding
`BIND_ADDR=0.0.0.0`.

## Backup / restore
`POST /api/admin/backup` writes `knowledge-<ts>.db` (VACUUM INTO) under `/data/backups`.
Restore: stop the container and copy a backup over `/data/knowledge.db` in the volume.
Notes are also mirrored as markdown in `/data/notes`. Do not copy the live `.db` file.

## Extras (phase 4)
- Inbox import: drop `.md`/`.txt` in `/data/inbox`, then `POST /api/import/scan` (`{"preprocess":true}` sends to the AI review queue).
- `POST /api/admin/mirror/rebuild`, `GET /api/admin/export` (zip), `POST|GET /api/admin/reindex`.
- Remote embeddings: `EMBED_BACKEND=openai`, `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_MODEL`, then reindex.
- MCP bridge: `docker compose --profile mcp up -d` (streamable HTTP on :8000) or
  `HUB_URL=... API_TOKEN=... MCP_TRANSPORT=stdio python -m hub_mcp`.
- Off-site backup: `docker compose --profile backup up -d` syncs `/data/notes` and `/data/backups`
  with rclone; run `docker compose run --rm backup config` once to set up `RCLONE_REMOTE`.
