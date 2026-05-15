## Context

Today, publishing an episode is a manual, error-prone sequence: SCP the source, run `./pipeline.sh`, hand-write an `EpisodeInfo` JSON, copy the key base64 out of `out/keys/<ep>.key.b64`, and point the Android SDK at the playlist. The existing stage scripts (`generate-drm-key.sh` → `encode-clear.sh` → `encrypt-segments.sh`) are battle-tested and must not be modified — they embed a load-bearing workaround for FFmpeg's fMP4+AES bug (documented at the top of `encode-clear.sh`). `server.py` is only a stdlib `SimpleHTTPServer` + CORS header; it can neither parse multipart uploads nor do async work.

The downstream consumer is the `media3-shortdrama` Android SDK, whose `EpisodeSource` contract is `episode-info-schema.json`. Phase-1 requirements include DRM fast-start — the client receives `keyBase64` + `ivHex` in the episode metadata and pre-fills `DrmKeyStore`, so the player never blocks on the key HTTP request. The `#EXT-X-KEY:URI` in the playlist is still required to be a real URL: hls.js in the browser and AVPlayer on Safari don't use the pre-fill path and do HTTP GET the key.

Traffic profile: internal company network, small team, a handful of uploads per day, single-digit concurrent viewers. Source files are ≤ a few hundred MB. Running the pipeline on three ladder rungs with `libx264 -preset veryfast` is CPU-bound; a single active encode at a time is tolerable and avoids contention between jobs.

## Goals / Non-Goals

**Goals:**
- Make "publish an episode" a single form submission that returns in seconds.
- Produce `EpisodeInfo` JSON deterministically from the pipeline's own outputs — no manual transcription.
- Preserve the existing pipeline scripts and filesystem layout verbatim.
- Surface pipeline failure state in the admin UI with enough stderr context to diagnose without SSH.
- Keep the whole thing deployable as one Python process + one SQLite file.

**Non-Goals:**
- Adaptive bitrate / master playlist (SDK is deliberately single-rung).
- Authentication, authorization, audit logging (internal-only deployment).
- Horizontal scaling, distributed queues, or multi-node deployment.
- Rewriting or refactoring `pipeline.sh` and its stage scripts.
- Upload progress bars, chunked/resumable uploads, parallel encodes.
- Automatic cleanup of failed-run artifacts (keep them for post-mortem).
- A "reset cover to auto" button (out of Phase 1 scope).

## Decisions

### D1. FastAPI + SQLite + Uvicorn (single process)

**Decision**: Build the service as a FastAPI app running under Uvicorn, with SQLite for state and Jinja2 for the admin HTML page.

**Why**: FastAPI gives us native async, automatic OpenAPI (useful as internal docs), typed request/response models via Pydantic, first-class multipart handling via `python-multipart`, and `BackgroundTasks` / dependency injection. SQLite avoids operational burden (no daemon to babysit) and is far more than enough for the expected row count. Jinja2 is the lightest templating option that ships naturally with FastAPI.

**Alternatives considered**:
- *Flask*: simpler but no native async; background jobs become awkward. Not worth it.
- *Continue stdlib `http.server`*: multipart parsing, async queueing, and JSON validation would all need to be hand-rolled. 5× the code.
- *Node.js / Go rewrite*: would abandon the Python ecosystem shared with `pipeline.sh`'s invocation. No upside for this scale.

### D2. Single global async queue for pipeline execution

**Decision**: One `asyncio.Queue` created at app startup, drained by exactly one worker coroutine spawned in the FastAPI lifespan. Uploads land rows with `status=pending` then `await queue.put(job)`. The worker pulls one job, flips `status=encoding`, runs `pipeline.sh` via `asyncio.create_subprocess_exec`, and updates the row to `ready` or `failed`.

**Why**: Three ladder rungs + libx264 saturates CPU; running two simultaneous jobs makes both slower than running them in sequence (lock contention on the CPU) and destabilizes latency. A single in-process queue is the simplest implementation of FIFO serialization, zero dependencies, zero deployment surface. Loss of queue state on restart is acceptable: the `pending` / `encoding` rows are visible in the admin page and can be re-uploaded.

**Alternatives considered**:
- *Celery / RQ / Dramatiq with Redis*: massive overkill for "one worker, rare jobs".
- *Threaded `concurrent.futures.ThreadPoolExecutor(max_workers=1)`*: works but fights FastAPI's async model; `create_subprocess_exec` is cleaner.

**Consequence**: a process restart mid-encode leaves an `encoding` row orphaned. On startup the service SHALL detect any row with `status=encoding` and flip it to `failed` with `error_message="orphaned by restart"`. Operators can re-upload.

### D3. Synchronous cover + duration extraction before the upload response

**Decision**: Run `ffprobe` (for `duration_ms`) and `ffmpeg -ss 0 -vframes 1 -vf scale=-2:720` (for `cover.jpg`) **inline** in the upload handler, before the DB insert and before the 302 response. Only the three-rung encode is pushed to the queue.

**Why**: Both operations complete in under a second on modern hardware for typical inputs (frame extraction reads the first keyframe; `ffprobe` reads only headers). Keeping them synchronous means the admin page renders the row with a real thumbnail immediately — no placeholder, no polling, no second "cover ready" state to track. The response latency cost is negligible next to the upload itself.

**Alternatives considered**:
- *Defer cover to the queue worker*: forces the UI to poll / show a placeholder; doubles the number of state transitions.
- *Probe duration from the final m3u8* (sum of `#EXTINF`): fine but means duration is unknown until encode finishes; that breaks the SDK contract that `durationMs` is always present as soon as the episode exists.

### D4. Key URL is real; SDK fast-start bypasses it

**Decision**: Write a concrete URL into `#EXT-X-KEY:URI` — specifically `{PUBLIC_BASE_URL}/drm/{drama_slug}/ep-{n}/key` — and serve the 16 raw bytes there. In parallel, return `drm.keyBase64` + `drm.ivHex` inside the `GET /api/episodes/{slug}/{ep}` response so the `media3-shortdrama` SDK can pre-fill `DrmKeyStore` and skip the HTTP round-trip at play time.

**Why**: The schema's keyUri docstring requires it to match the playlist verbatim and serve as the `DrmKeyStore` lookup key. Players that don't have pre-fill (hls.js in the browser, AVPlayer on Safari) will only work if that URL actually serves the key. Embedding the base64 key in the episode-info response is the Phase-1 "DRM fast-start" feature.

**Security note**: both paths are unauthenticated. This is consistent with "internal network, no auth" (D6). In a public deployment this design would not be acceptable — it's explicitly fine here because the service never leaves the VPN.

### D5. Explicit `drama_slug` separate from `drama_name`

**Decision**: The user enters both fields at upload time. `drama_slug` is validated against `^[a-z0-9][a-z0-9-]*$` and used for: directory name (`out/{slug}/`), all URLs, and the `episode_id` composition (`{slug}-ep-{n}`). `drama_name` is the human-friendly label (Chinese OK) shown in the admin UI.

**Why**: Passing arbitrary Chinese input to a shell command as a directory name is a path-traversal / shell-injection hazard and produces fragile URLs. Auto-generating a slug with a pinyin library introduces a heavy dependency and non-obvious collisions. Making the operator choose a slug once, explicitly, is lower cost in total.

**Alternatives considered**:
- *Auto-slug via pinyin*: dependency on `pypinyin`, collision rules get surprising (同音字 / polyphones).
- *Auto-slug as `drama-001` incrementing*: opaque URLs, harder to eyeball in logs.

### D6. No authentication

**Decision**: No auth on any endpoint — admin page, upload, cover replacement, key fetch, static files.

**Why**: The deployment target is an internal VPN-only network. Running basic-auth or an SSO integration buys nothing against the actual threat model (a rogue internal actor can also go read the files directly on the host) and adds friction. This decision is reversible: if the service ever needs to be exposed, wrap it in a reverse proxy with auth — no code changes.

**Mitigation baked in**: the `/videos/` static mount SHALL not expose `keys/`; directory listings are disabled. The DRM fast-start pattern means the "key endpoint" is rarely hit, so a network-level ACL on `/drm/` is an easy add-on.

### D7. Overwrite-on-reupload, no artifact cleanup on failure

**Decision**: Re-uploading the same `(drama_slug, ep_number)` updates the existing row in place and re-runs the pipeline. `ffmpeg -y` in `encode-clear.sh` overwrites the ladder outputs, and `encrypt-segments.sh` re-encrypts the fresh outputs; no stale data leaks between runs. When a run fails, artifacts under `out/{slug}/ep-{n}/` are preserved so a human can inspect them.

**Why**: Overwrite is how operators think about "I messed up, let me fix that". "Keep failed artifacts" is cheap insurance against "the encode broke for a reason I need to see".

**The one subtle scenario**: if a failed run stops partway through `encrypt-segments.sh` (some `.m4s` encrypted, some not), the mixed state is overwritten by the next successful run because stage 1 regenerates every `.m4s` from scratch before stage 2 encrypts. So there's no corrupted-output risk from the combination of D7 choices.

### D8. Configuration via env, fail-fast on bad base URL

**Decision**: `PUBLIC_BASE_URL` is required; startup fails if missing or if it doesn't parse as an absolute http(s) URL. One trailing slash is stripped on load (so both `http://host:8000` and `http://host:8000/` work). `OUT_DIR`, `DB_PATH`, `UPLOAD_TMP_DIR` have sensible defaults.

**Why**: `PUBLIC_BASE_URL` flows into three durable artifacts — the `#EXT-X-KEY:URI` baked into the m3u8, the `playUrl` stored in the DB, and the `coverUrl` returned to clients. Silently computing these from a missing / wrong base URL at upload time produces broken episodes that only manifest at playback. Fail early instead.

## Implementation sketch

```
app/
├── main.py              # FastAPI() factory, lifespan spawns worker
├── config.py            # env var load + validation (PUBLIC_BASE_URL, OUT_DIR, ...)
├── db.py                # connection, schema init, CRUD helpers
├── models.py            # Pydantic response models (EpisodeInfo, AdminRow)
├── queue.py             # asyncio.Queue singleton + worker coroutine
├── pipeline.py          # subprocess wrapper around ./pipeline.sh
├── ffmpeg_utils.py      # ffprobe duration, ffmpeg first-frame extract
├── routers/
│   ├── admin.py         # GET /admin (HTML), POST /admin/upload, GET /admin/episodes
│   ├── api.py           # GET /api/episodes/{slug}/{ep}, POST /api/.../cover
│   └── drm.py           # GET /drm/{slug}/{ep}/key
├── templates/
│   └── admin.html
└── static/              # optional CSS/JS
```

Static mount `/videos/` → `OUT_DIR` with `html=False`, `check_dir=True`, and an explicit path filter that denies `keys/`.

## Risks / Trade-offs

- **[Risk] Process restart mid-encode orphans `encoding` rows** → Mitigation: startup scan flips any `status=encoding` row to `failed` with `error_message="orphaned by restart"`.
- **[Risk] Very large uploads exhaust disk in `UPLOAD_TMP_DIR` / `OUT_DIR`** → Mitigation: none in code. Document a disk-usage expectation and rely on operator monitoring (internal-only scope).
- **[Risk] Chinese / emoji in `drama_name` breaks log parsing** → Mitigation: always log `drama_slug` (ASCII) alongside `drama_name`; keep `drama_name` only for UI rendering.
- **[Risk] Overwrite on re-upload silently nukes the old run's DRM key, invalidating any already-distributed `EpisodeInfo` cached by clients** → Mitigation: documented as explicit semantic of overwrite; operators should communicate a re-encode to downstream consumers. Not a code-level mitigation in Phase 1.
- **[Risk] `pipeline.sh` is bash + GNU/BSD tooling; path/locale surprises on non-macOS hosts** → Mitigation: document required tools in `CLAUDE.md`; smoke-test on the target deployment host before first use.
- **[Risk] Disk race — two simultaneous uploads for different `(slug, ep)` write to `OUT_DIR` concurrently via the worker** → N/A because D2 serializes. Synchronous cover/duration extraction in the upload handler runs in parallel across requests, but each handler writes to a distinct `{slug}/ep-{n}/` subdirectory, so there's no contention.
- **[Trade-off] No auth means anyone on the internal network can delete data** → Accepted per D6.
- **[Trade-off] SQLite caps effective write concurrency to one in practice** → Accepted; write load is dominated by the serialized pipeline, not the upload path.

## Migration Plan

- No existing production state to migrate.
- Delete `server.py` at cutover. Document the new `uvicorn app.main:app --host 0.0.0.0 --port 8000` command in `CLAUDE.md`.
- `index.html` at repo root is test-only; leave it untouched. It can be repointed at `/videos/ep1/720p/media-720p.m3u8` once at least one episode is published through the service.
- Schema update (`episode-info-schema.json` gains `coverUrl`) is additive; no consumer migration needed.

## Open Questions

- Should the admin page auto-refresh the list (poll `/admin/episodes` every N seconds) so operators see `encoding → ready` without reloading? Defer until user feedback.
- Should `/drm/.../key` emit a `Cache-Control: no-store` header to avoid CDN caching? Not an issue today (no CDN in front of the internal service) but worth noting if that ever changes.
- Whether to expose the `*.key.b64` / `*.iv` files over HTTP at all. Current decision: no — they leak the raw key if the admin auth story ever changes. Only the binary `.key` is served, and only via `/drm/...`.
