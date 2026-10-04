# Personal Knowledge Hub

Self-hosted, single-user notes with hybrid (keyword + semantic) search. Spec: `docs/BUILD_SPEC.md`.
Status: phases 0-1 (notes, search, UI, backup).

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
