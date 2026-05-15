## Why

The repo currently ships a shell-based HLS encryption test bench (`pipeline.sh` + three stage scripts) and a stdlib CORS static file server (`server.py`). Operations staff who want to publish a new short-drama episode have to SSH in, scp the source, hand-edit arguments, run `./pipeline.sh`, and manually draft the `EpisodeInfo` JSON the Android SDK (`media3-shortdrama`) consumes. This change introduces a small internal web service that wraps the existing pipeline so that publishing an episode is a form submission and the SDK contract is produced deterministically — without replacing or re-implementing the pipeline itself.

## What Changes

- Add a FastAPI + SQLite + Uvicorn service that serves a management web page, accepts episode uploads (fields: `drama_slug`, `drama_name`, `ep_number`, `video`), persists metadata, and exposes SDK/admin HTTP endpoints.
- On upload: validate `drama_slug` (`^[a-z0-9][a-z0-9-]*$`), read `durationMs` via `ffprobe`, extract a first-frame JPEG cover via `ffmpeg` **before** responding, insert/overwrite a DB row with `status=pending`, and enqueue an async task.
- Run `pipeline.sh` via a single-worker `asyncio.Queue` consumer (globally serialized — no concurrent ffmpeg runs). On success persist `key_uri / key_b64 / iv_hex / play_url` and flip `status=ready`; on failure flip `status=failed` and store the stderr tail in `error_message`.
- Serve `GET /api/episodes/{slug}/{ep}` returning an object that strictly matches `episode-info-schema.json` — including `drm.keyBase64` and `drm.ivHex` embedded for DRM fast-start so the player can pre-fill `DrmKeyStore` and skip the HTTP key round-trip. Non-`ready` records return **404**.
- Serve `GET /drm/{drama_slug}/{ep}/key` (16 raw bytes, `application/octet-stream`) as the real URL written into `#EXT-X-KEY:URI`. Required as fallback for players that don't pre-fill (hls.js, Safari).
- Static-mount `out/{drama_slug}/{ep}/**` under `/videos/{drama_slug}/{ep}/**` so `.m3u8`, `.m4s`, `init-*.mp4`, and `cover.jpg` are directly reachable.
- Serve the management page at `GET /admin`: upload form + episode list sorted by `created_at desc` with cover thumbnails, status, error info, and a click-to-replace cover interaction.
- Add `POST /api/episodes/{slug}/{ep}/cover` (multipart) for manual cover replacement; writes over the existing JPEG in place and bumps `updated_at`.
- **BREAKING (schema)**: extend `episode-info-schema.json` with an optional `coverUrl: string | null` property. Existing SDK consumers remain compatible because the field is optional, but publishers that validate against the old schema will need to pick up the new version.
- Retire `server.py` — the new FastAPI app replaces it for local playback as well.
- Out of scope (explicitly deferred): ABR/master playlist, user accounts/permissions, pagination, upload progress/resume, auto-cleanup of failed pipeline artifacts, a "reset cover to auto" button, any change to `pipeline.sh` / stage scripts themselves.

## Capabilities

### New Capabilities
- `hls-management-server`: end-to-end management-server behavior — upload intake (including synchronous cover extraction and duration probing), persistence with `drama_slug` + `ep_number` uniqueness, single-worker async pipeline orchestration, status lifecycle, SDK episode-info endpoint, admin list endpoint, DRM key endpoint, cover replacement endpoint, static hosting of pipeline artifacts, and the admin HTML page.

### Modified Capabilities
<!-- No existing openspec/specs/ capabilities exist yet; the episode-info-schema.json extension is covered within the new hls-management-server spec (the "SDK episode-info response" requirement), since no prior capability owned it. -->

## Impact

- **New files**: `app/` Python package (FastAPI app, SQLite models, routers, background worker, Jinja2 templates), `requirements.txt` (or `pyproject.toml`), startup docs in `CLAUDE.md`.
- **Modified files**: `episode-info-schema.json` gains `coverUrl`. `CLAUDE.md` gains a section on running the management server and on the upload → pipeline lifecycle.
- **Removed files**: `server.py` (superseded).
- **New runtime dependencies**: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `aiosqlite` (or `sqlite3` stdlib via thread-pool); system deps `ffmpeg` + `ffprobe` already required by `pipeline.sh`.
- **New runtime configuration**: env var `PUBLIC_BASE_URL` (required — used verbatim in `playUrl`, `coverUrl`, and `#EXT-X-KEY:URI`); env var `OUT_DIR` (default `./out`); env var `DB_PATH` (default `./hls.db`); env var `UPLOAD_TMP_DIR` (default `./tmp`).
- **Filesystem layout unchanged** except that `out/` now contains a `{drama_slug}/{ep}/cover.jpg` alongside the rungs. Key files still live at `out/keys/{ep_id}.key` (pipeline stage 0 behavior preserved).
- **SDK consumers**: Android `media3-shortdrama` clients continue to work unmodified. Clients that want to show covers opt-in by reading the new `coverUrl` field.
- **Security posture**: the service trusts its network — no auth on upload, key endpoint, or admin page. Deployment must stay behind VPN/internal network (documented in `CLAUDE.md`).
