# Public Release Cleanup

This document outlines the sanitization and cleanup audit performed to prepare the repository for public release, ensuring no sensitive data, private credentials, or internal infrastructure details are exposed.

## Summary of Cleanup Actions

1. **Removed Internal Development Scratchpad**
   - Removed `knowledge_hub_discussion.md` from the repository. This file was an internal scratchpad containing development discussions and internal LAN IP addresses.

2. **Hardened `.gitignore` and `.dockerignore`**
   - Added explicit patterns to prevent accidental tracking or packaging of runtime database files, write-ahead logs, and local SQLite stores:
     - `data/`
     - `*.db`
     - `*.db-shm`
     - `*.db-wal`
   - Verified that `.dockerignore` prevents any local databases or temporary data directories from being copied into container build contexts.

3. **Secret & Credential Audit**
   - Conducted an automated and manual scan across the repository for credentials, private keys, API keys, and sensitive tokens.
   - Confirmed that all token references in configuration samples and docker-compose files are explicit placeholders (e.g., `HUB_TOKEN=change-me` or test-specific fixtures).
   - Confirmed that no real secrets or production credentials exist in the codebase.

4. **Network & Address Sanitization**
   - Verified all documentation and default configs only reference loopback/internal Docker network addresses (such as `http://hub:5000`, `http://localhost:5000`, or `http://127.0.0.1:5000`).

5. **Dependency Audit**
   - Audited dependencies against the GitHub Advisory Database for known vulnerabilities.
   - Pinned `httpx==0.28.1` in `requirements-mcp.txt` for the MCP bridge container.

## T7 Public-ready Pass

- Git history scanned (full clone, all branches) for keys, tokens, private keys, emails and LAN IPs. Only finding: LAN IP `192.168.1.28` in the removed `knowledge_hub_discussion.md` (commits `cda7dd5` added it, `d0d1563` removed it, plus intermediate edits). A PEM header string in `tests/test_guard.py` is a synthetic fixture. No history rewrite was done; the owner should decide whether to rewrite.
- Real pet names in docs, skill and tests replaced with neutral ones.
- Auth hardened (`docs/DECISIONS.md`), `SECURITY.md` added, CI adds gitleaks and no-push image builds, publish runs only on `v*` tags, actions SHA-pinned.
- License: MIT (already present); the choice stays with the owner.
