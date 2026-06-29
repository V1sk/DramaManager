# Repository Guidelines

## Project Structure & Module Organization

This repository is a FastAPI-based HLS management server. Core code lives in `app/`: `main.py` wires the service, `routers/` contains route groups, `db.py` manages SQLite state, and `work_queue.py` coordinates encode jobs. Templates are in `app/templates/`, browser assets in `app/static/`, and storage integrations in `app/storage/`. The media pipeline is split across `pipeline.sh`, `generate-drm-key.sh`, `encode-clear.sh`, and `encrypt-segments.sh`. Tests live in `tests/`; OpenSpec history and active specs are under `openspec/`.

## Build, Test, and Development Commands

- `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`: create a local Python environment.
- `./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000`: run the management server locally.
- `./venv/bin/pytest`: run the automated test suite.
- `docker compose up -d --build`: build and run the deployment container on port `8000`.
- `docker compose logs -f hls`: follow container logs.
- `./pipeline.sh <source.mp4> <output_dir> <episode_id> <key_uri>`: debug the encode/encrypt pipeline directly.

Native pipeline dependencies include `ffmpeg`, `ffprobe`, `openssl`, `xxd`, `bash`, and `awk`.

## Coding Style & Naming Conventions

Use Python 3.11-compatible code with 4-space indentation and descriptive snake_case names for functions, modules, variables, and route helpers. Keep route files grouped by feature in `app/routers/`. Preserve shell script stage boundaries; ladder metadata belongs in `pipeline.sh`, not duplicated inside stage scripts. Prefer explicit validation and fail-fast configuration checks in `app/config.py`.

## Testing Guidelines

Tests use `pytest` and follow the `tests/test_*.py` naming pattern. Add focused tests near the behavior being changed, especially for database migrations, URL payloads, retry/resume logic, storage provider modes, and media metadata parsing. `tests/conftest.py` supplies required auth bootstrap environment defaults; avoid relying on local `.env` state.

## Commit & Pull Request Guidelines

Recent commits use bracketed conventional-style prefixes, for example `[feat]: ...`. Follow that pattern with concise, imperative summaries such as `[fix]: prevent stale sync payload`. Pull requests should describe the user-visible change, note config or migration impact, list test commands run, and include screenshots for admin UI changes.

## Security & Configuration Tips

Do not commit secrets. Use `.env` for deployment settings and copy `app/storage/credentials.example.py` to `app/storage/credentials.py` locally when OSS/TOS credentials are needed. The SDK-facing `/api/*`, `/drm/*`, and `/videos/*` routes are intentionally unauthenticated and must stay behind an internal network or VPN.

## Agent-Specific Instructions

Preserve the single-process server model: do not change Docker or uvicorn to use multiple workers, because the encode queue and per-episode locks are in-process. Keep pipeline stages separate; `init.mp4` remains clear, encrypted segments are produced in `encrypt-segments.sh`, and ladder metadata belongs in `pipeline.sh`. Do not break SDK URL contracts: preview/API payloads use host-relative `/videos/...` and `/drm/...` paths. Re-uploaded episodes must use versioned paths such as `ep-1-v2` to avoid stale client caches. NAS source files are source-of-truth inputs and must not be deleted like upload temp files. Treat staging-to-prod business sync as an explicit operator action. For substantial behavior changes, check `openspec/` first and update the relevant proposal, tasks, or spec.
