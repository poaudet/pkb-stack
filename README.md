# Personal Knowledge Hub (`pkb-stack`)

A self-hosted, privacy-first personal knowledge base with hybrid search, structured entity tracking, and native Model Context Protocol (MCP) support for AI agents.

[![CI](https://github.com/poaudet/pkb-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/poaudet/pkb-stack/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![MCP](https://img.shields.io/badge/MCP-2.3.0-green.svg)](https://modelcontextprotocol.io/)

---

## Overview

The **Personal Knowledge Hub** is designed as a single-user, long-term personal knowledge and memory store. It combines the flexibility of human-readable Markdown notes with the precision of structured, queryable entities (people, projects, events, messages, memories, and custom schemas) and gives AI assistants (Claude, Cursor, Copilot, etc.) safe, audited access to your knowledge graph via MCP.

### Key Features

- **Hybrid Search Engine**: Blends SQLite FTS5 (BM25 keyword search) with vector embeddings (`sqlite-vec`) using Reciprocal Rank Fusion (RRF) for fast, highly relevant search results.
- **Structured Entities & Markdown Notes**: Store freeform notes with bi-directional wikilinks (`[[Note Name]]`) alongside strongly-typed, schema-validated entities with uniqueness constraints and audit trails.
- **Model Context Protocol (MCP) Bridge**: First-class MCP server (`hub_mcp`) supporting both Streamable HTTP (SSE) and standard I/O (`stdio`) transports, allowing AI agents to query and curate knowledge seamlessly.
- **AI Smart Capture & Tidy-Up**: Optional background processing via local (Ollama) or remote (OpenAI-compatible) LLMs to decompose unstructured notes into structured records.
- **Zero Lock-In & Data Sovereignty**:
  - Live markdown mirroring under `/data/notes` keeps your notes accessible in plain text.
  - Pure SQLite storage (`knowledge.db`) using stdlib `sqlite3` in WAL mode—no heavy ORMs.
  - Online, non-blocking backups via `VACUUM INTO` and ZIP exports.
  - Automated offsite backup profile using `rclone`.

---

## Quick Start (Docker Compose)

### 1. Clone & Configure

```bash
git clone https://github.com/poaudet/pkb-stack.git
cd pkb-stack

cp .env.example .env
```

Review `.env` to set your desired options. If you expose the Hub beyond localhost (`127.0.0.1`), configure a strong `API_TOKEN`.

### 2. Launching Services (Docker Compose)

The repository provides focused Docker Compose configurations depending on your setup needs:

#### Option 1: Knowledge Hub Only
To run only the Knowledge Hub service:

```bash
docker compose -f docker-compose.hub.yml up -d --build
```

#### Option 2: Knowledge Hub + MCP Bridge (Automatic Dependency)
To run both the Knowledge Hub and the Model Context Protocol (MCP) server together—where the MCP service automatically starts and depends on the Hub without requiring profiles:

```bash
docker compose -f docker-compose.hub-mcp.yml up -d --build
# Or equivalently:
docker compose -f docker-compose.mcp.yml up -d --build
```

#### Option 3: Full Stack with Optional Profiles
You can also use the default `docker-compose.yml` file, which includes optional profiles for MCP, local LLMs, and automated backups:

```bash
# Start just the hub:
docker compose up -d --build

# Start the hub and MCP bridge:
docker compose --profile mcp up -d --build
```

Open the Web UI:
```bash
open http://127.0.0.1:5000
```

### 3. Optional Compose Profiles

The stack includes optional profiles for additional capabilities:

- **MCP Bridge** (`--profile mcp`):
  Runs the Model Context Protocol HTTP service on port 8000:
  ```bash
  docker compose --profile mcp up -d
  ```

- **Local LLM** (`--profile local-llm`):
  Runs an Ollama container for local embeddings and AI tidy-up:
  ```bash
  docker compose --profile local-llm up -d
  ```

- **Off-Site Backup** (`--profile backup`):
  Runs continuous `rclone` synchronization for `/data/notes` and `/data/backups`:
  ```bash
  # Configure rclone remote interactively first:
  docker compose run --rm backup config
  # Start the sync service:
  docker compose --profile backup up -d
  ```

---

## Model Context Protocol (MCP)

The repository includes a dedicated MCP bridge (`hub_mcp`) implementing the [Model Context Protocol](https://modelcontextprotocol.io/). It exposes Hub operations as standard tools with schema validation, permission checks, and actor tracking.

### Available MCP Tools

| Tool | Description |
|---|---|
| `search` | Hybrid search across notes and entities (query, limit, entity_type filter). |
| `read_note` | Retrieve note content, metadata, and revision info by ID. |
| `save_note` | Create a new note or supersede an existing note with new content. |
| `list_recent_notes`| Retrieve a list of recently created or modified notes. |
| `read_entity` | Retrieve a structured entity by UID or unique key. |
| `save_entity` | Create or update a structured entity with schema validation. |
| `find_entities` | Search and filter entities by type and JSON field criteria. |
| `get_self` | Retrieve profile information about the knowledge base owner. |
| `list_types` | List all available entity types and schemas. |

### Connecting to Claude Desktop / Cursor

#### Option A: Stdio Transport (Direct or Docker)

Add the server to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "knowledge-hub": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
        "--network", "host",
        "-e", "HUB_URL=http://127.0.0.1:5000",
        "-e", "API_TOKEN=your-token-if-set",
        "-e", "MCP_TRANSPORT=stdio",
        "ghcr.io/poaudet/pkb-stack/mcp:latest"
      ]
    }
  }
}
```

Or run via Python in a local environment:

```json
{
  "mcpServers": {
    "knowledge-hub": {
      "command": "python",
      "args": ["-m", "hub_mcp"],
      "env": {
        "HUB_URL": "http://127.0.0.1:5000",
        "API_TOKEN": "your-token-if-set",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

#### Option B: Streamable HTTP (SSE) Transport

When running via `docker compose --profile mcp up -d`, the MCP server listens on port `8000` (configurable via `MCP_PORT`). Point your MCP-compatible client to:

```
http://127.0.0.1:8000/mcp
```

---

## Configuration & Environment Variables

Key variables defined in `.env.example`:

| Variable | Default | Purpose |
|---|---|---|
| `BIND_ADDR` | `127.0.0.1` | Network interface to bind (use `0.0.0.0` only with `API_TOKEN`). |
| `PORT` | `5000` | Port for the Hub web server. |
| `API_TOKEN` | _empty_ | ****** required for API / tool requests when set. |
| `EMBED_BACKEND` | `local` | Embedding backend: `local` (fastembed), `openai`, or `fake` (testing). |
| `EMBED_MODEL` | `intfloat/multilingual-e5-small` | Hugging Face model or OpenAI embedding model name. |
| `MIRROR_MARKDOWN` | `true` | Maintain human-readable `.md` mirrors under `/data/notes`. |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` or `stdio`. |
| `MCP_PORT` | `8000` | Port for MCP Streamable HTTP service. |
| `CAPTURE_ENABLED` | `true` | Enable Smart Capture pipeline for automated entity extraction. |
| `LLM_MODEL` | _empty_ | Model name for AI note tidy-up and entity extraction. |

---

## Backup & Recovery

1. **Online Database Backup**:
   Trigger an atomic snapshot without stopping the server:
   ```bash
   curl -X POST http://127.0.0.1:5000/api/admin/backup \
     -H "Authorization: ******"
   ```
   Backups are written as `knowledge-<timestamp>.db` to `/data/backups/`.

2. **Restore**:
   Stop the container and replace `/data/knowledge.db` with your snapshot file.

3. **Markdown Mirror**:
   All active notes are maintained as plain `.md` files with YAML front matter in `/data/notes/`. If you ever need to rebuild the database from the markdown mirror:
   ```bash
   curl -X POST http://127.0.0.1:5000/api/admin/mirror/rebuild \
     -H "Authorization: ******"
   ```

4. **ZIP Export**:
   Download a full archive of all notes and metadata:
   ```bash
   curl -O http://127.0.0.1:5000/api/admin/export \
     -H "Authorization: ******"
   ```

---

## Development & Testing

### Local Setup

Requires Python 3.11+.

```bash
python3.11 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt -r requirements-mcp.txt -r requirements-dev.txt
```

### Running Checks

```bash
# Run code style and linter checks
ruff check .

# Run test suite (using fast fake embeddings)
EMBED_BACKEND=fake pytest -q

# Run end-to-end smoke test against Docker stack
scripts/smoke.sh
```

---

## Container Registry & CI/CD

Pre-built Docker images are published to the GitHub Container Registry (GHCR) upon release:

- **Hub**: `ghcr.io/poaudet/pkb-stack/hub:latest`
- **MCP Bridge**: `ghcr.io/poaudet/pkb-stack/mcp:latest`

Automated workflows:
- `.github/workflows/ci.yml`: Runs linter, test suite, and image build checks on pull requests and pushes.
- `.github/workflows/publish.yml`: Builds and pushes multi-tag images to GHCR on releases, version tags, and main branch updates.

---

## Security & Privacy Notice

- **Single-User Architecture**: Designed for personal data storage. Authentication is enforced via a single shared bearer token (`API_TOKEN`).
- **Network Isolation**: By default, services bind to `127.0.0.1`. Never expose the application directly to the public internet without an authenticating reverse proxy (e.g. Caddy, Traefik, Nginx) enforcing HTTPS.
- **Sensitive Content Logging**: The application explicitly suppresses note content and entity bodies from logs above `DEBUG` level.
- **Database Safety**: Raw runtime databases (`*.db`, `*.db-wal`) and data directories are excluded from git tracking and container images via `.gitignore` and `.dockerignore`.
