#!/usr/bin/env bash
# End-to-end smoke test against the docker compose stack.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-5000}"
BASE="http://127.0.0.1:${PORT}"
AUTH=()
[ -n "${API_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${API_TOKEN}")
cleanup() { docker compose down; }
trap cleanup EXIT

docker compose up -d $([ "${SMOKE_BUILD:-1}" = 1 ] && echo --build)
for _ in $(seq 1 60); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$BASE/health"; echo

jget() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
req() { curl -fsS "${AUTH[@]}" -H 'Content-Type: application/json' "$@"; }

ID1=$(req -X POST "$BASE/api/notes" -d '{"content":"Smoke test: the pelican flies at dawn"}' | jget 'd["note"]["id"]')
req -X POST "$BASE/api/search" -d '{"q":"pelican"}' | jget 'd["hits"][0]["id"]' | grep -q "$ID1"
ID2=$(req -X POST "$BASE/api/notes/$ID1/supersede" -d '{"content":"Smoke test: the heron flies at dusk"}' | jget 'd["note"]["id"]')
req -X POST "$BASE/api/search" -d '{"q":"pelican"}' | jget '[h["id"] for h in d["hits"]]' | grep -qv "$ID1"
req -X DELETE "$BASE/api/notes/$ID1?confirm=true" >/dev/null
req -X DELETE "$BASE/api/notes/$ID2?confirm=true" >/dev/null
req "$BASE/api/stats"; echo
echo "smoke OK"
