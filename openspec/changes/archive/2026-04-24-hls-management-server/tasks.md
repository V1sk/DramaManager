## 1. Infrastructure — FastAPI skeleton, config, DB, static mount

- [x] 1.1 Add `requirements.txt` with `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart` (and pin versions); document `ffmpeg` + `ffprobe` as system prerequisites in `CLAUDE.md`.
- [x] 1.2 Create `app/` package layout per the design sketch (`main.py`, `config.py`, `db.py`, `models.py`, `queue.py`, `pipeline.py`, `ffmpeg_utils.py`, `routers/`, `templates/`, `static/`).
- [x] 1.3 Implement `app/config.py`: load `PUBLIC_BASE_URL` (required, validate absolute http/https, strip exactly one trailing slash), `OUT_DIR` (default `./out`), `DB_PATH` (default `./hls.db`), `UPLOAD_TMP_DIR` (default `./tmp`); fail fast on bad `PUBLIC_BASE_URL`.
- [x] 1.4 Implement `app/db.py`: SQLite connection helpers, `episodes` table DDL matching the schema in proposal (unique `(drama_slug, ep_number)`, unique `episode_id`), CRUD helpers (`upsert_episode`, `set_status`, `list_episodes`, `get_episode`).
- [x] 1.5 Implement `app/main.py`: `FastAPI` app factory, Jinja2 templates wiring, `Access-Control-Allow-Origin: *` middleware, routers imported, lifespan hook that (a) runs DB init, (b) flips any row with `status=encoding` to `failed` with `error_message="orphaned by restart"`, (c) spawns the single worker coroutine.
- [x] 1.6 Mount static route `/videos` → `OUT_DIR` with directory listing disabled and an explicit filter rejecting any path whose first segment after `{drama_slug}/` equals `keys`.
- [x] 1.7 Add `GET /` → 302 redirect to `/admin`.
- [x] 1.8 Delete `server.py`; update `CLAUDE.md` with the new `uvicorn app.main:app --host 0.0.0.0 --port 8000` command and the required env vars.

## 2. Upload flow — validation, ffprobe, cover, persist

- [x] 2.1 Implement `app/ffmpeg_utils.py::probe_duration_ms(path)` invoking `ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1`, parse seconds → int ms; raise on non-zero exit.
- [x] 2.2 Implement `app/ffmpeg_utils.py::extract_first_frame(src, dst)` invoking `ffmpeg -y -ss 0 -i <src> -vframes 1 -vf scale=-2:720 <dst>`; raise on non-zero exit.
- [x] 2.3 Implement `POST /admin/upload` in `app/routers/admin.py`: accept multipart (`video`, `drama_slug`, `drama_name`, `ep_number`), validate `drama_slug` regex `^[a-z0-9][a-z0-9-]*$`, validate `ep_number` integer ≥ 1, validate non-empty trimmed `drama_name`, validate `video` file present.
- [x] 2.4 In the upload handler, stream the upload to a unique temp file under `UPLOAD_TMP_DIR` (`upload-<uuid>.mp4`); on any subsequent failure, delete the temp file.
- [x] 2.5 In the upload handler, call `probe_duration_ms` and `extract_first_frame` synchronously; target path `OUT_DIR/{drama_slug}/ep-{n}/cover.jpg` (create the directory).
- [x] 2.6 Upsert the `episodes` row: compose `episode_id = "{drama_slug}-ep-{n}"`, set `cover_url = "{PUBLIC_BASE_URL}/videos/{slug}/ep-{n}/cover.jpg"`, set `duration_ms`, set `source_filename`, set `status = "pending"`, clear `error_message`, set `created_at` (only on insert) and `updated_at`.
- [x] 2.7 Enqueue the job `{episode_id, drama_slug, ep_number, tmp_path}` onto the global queue, then return HTTP 302 to `/admin`.
- [x] 2.8 Handle duplicate `(drama_slug, ep_number)` as overwrite (update-in-place, not a new row); preserve `created_at`, refresh `updated_at`.

## 3. Async worker — pipeline execution, stderr capture, final persist

- [x] 3.1 Implement `app/queue.py`: module-level `asyncio.Queue`, worker coroutine that `await`s a job, flips `status=encoding`, runs the pipeline (see 3.2), flips `status=ready` or `failed`, then loops.
- [x] 3.2 Implement `app/pipeline.py::run_pipeline(source, out_dir, episode_id, key_uri)` using `asyncio.create_subprocess_exec` against `./pipeline.sh` with absolute paths; capture `stdout` and `stderr` concurrently; return `(returncode, stderr_tail_4kb)`.
- [x] 3.3 On successful exit, read `OUT_DIR/{slug}/keys/{episode_id}.key.b64` (strip whitespace) and `{episode_id}.iv` (strip whitespace); populate `key_b64`, `iv_hex`, `key_uri = "{PUBLIC_BASE_URL}/drm/{slug}/{episode_id}/key"`, `play_url = "{PUBLIC_BASE_URL}/videos/{slug}/{episode_id}/720p/media-720p.m3u8"`; set `status=ready`; clear `error_message`.
- [x] 3.4 On non-zero exit, set `status=failed`; write the last 4 KiB of combined stderr into `error_message`; do NOT delete artifacts under `OUT_DIR/{slug}/{episode_id}/`.
- [x] 3.5 On either outcome, delete the temp upload file; log the result with both `drama_slug` and `drama_name`.
- [x] 3.6 Ensure the worker survives individual job failures (exceptions during DB update, missing key files, etc.) by wrapping the job body in try/except that logs and continues.

## 4. SDK & admin HTTP APIs

- [x] 4.1 Implement `GET /api/episodes/{drama_slug}/{ep}` in `app/routers/api.py`: validate `ep` is digits, load row, 404 on missing or `status != ready`.
- [x] 4.2 Build the response via a Pydantic model mirroring `episode-info-schema.json` (required: `episodeId`, `playUrl`, `durationMs`; include `coverUrl`, `drm.keyUri`, `drm.keyBase64`, `drm.ivHex`; omit `initUrl`, `firstSegUrl`, `fallback` in Phase 1).
- [x] 4.3 Implement `POST /api/episodes/{drama_slug}/{ep}/cover`: accept multipart `cover`, validate MIME starts with `image/`, validate row exists (404 otherwise), overwrite `OUT_DIR/{slug}/ep-{n}/cover.jpg`, bump `updated_at`.
- [x] 4.4 Implement `GET /admin/episodes`: select all rows ordered by `created_at DESC`, return JSON array of admin-view objects (include `drama_slug`, `drama_name`, `ep_number`, `episode_id`, `status`, `duration_ms`, `play_url`, `cover_url`, `error_message`, `created_at`, `updated_at`).
- [x] 4.5 Implement `GET /drm/{drama_slug}/{ep}/key` in `app/routers/drm.py`: look up `OUT_DIR/{slug}/keys/ep-{ep}.key`, 404 if missing, return 16 raw bytes with `Content-Type: application/octet-stream` and `Content-Length: 16`; assert file size is exactly 16.

## 5. Admin web page

- [x] 5.1 Create `app/templates/admin.html`: upload form (drama_slug, drama_name, ep_number, video file inputs), client-side regex validation of `drama_slug`, submit handler that POSTs multipart to `/admin/upload`.
- [x] 5.2 Add the episode list section: on load, fetch `/admin/episodes`, render each row with cover thumbnail (from `cover_url`), drama_name, ep_number, status badge, duration (mm:ss), `created_at`, and — when `status == "failed"` — the `error_message` in a collapsible block.
- [x] 5.3 Make each cover thumbnail click-to-replace: hidden `<input type="file" accept="image/*">` per row, on change POSTs `multipart` to `/api/episodes/{slug}/{ep}/cover` and refreshes the thumbnail (cache-bust with `?t=<updated_at>`).
- [x] 5.4 Implement `GET /admin` in `app/routers/admin.py` that renders `admin.html`.

## 6. Schema update

- [x] 6.1 Edit `episode-info-schema.json`: add `coverUrl` to `properties` as `{"type": ["string", "null"], "format": "uri", "description": "Optional episode cover image URL (default first-frame JPEG; can be replaced via admin)"}`; do NOT add it to `required`.
- [x] 6.2 Update the schema's `examples` array to include `coverUrl` in at least the first example.
- [x] 6.3 Verify the existing second example (free content, `drm: null`, no `coverUrl`) still validates — it should, since `coverUrl` is optional.

## 7. Cross-cutting — docs and final validation

- [x] 7.1 Update `CLAUDE.md`: add a "Management server" section describing the startup command, env vars, URL map, and the upload→pipeline lifecycle; replace the now-stale `python3 server.py` reference.
- [x] 7.2 Smoke-test end-to-end on the dev host: upload `input.mp4` with `drama_slug=test`, `drama_name=测试`, `ep_number=1`; verify status transitions `pending → encoding → ready`; play the resulting m3u8 from the admin page's playUrl in `index.html` or VLC; verify `GET /api/episodes/test/1` validates against `episode-info-schema.json`; verify `GET /drm/test/ep-1/key` returns 16 bytes matching `out/test/keys/ep-1.key`.
- [x] 7.3 Verify the orphan-restart behavior: kill the process while a job is in `encoding`, restart, confirm the row flips to `failed` with `error_message="orphaned by restart"`.
- [x] 7.4 Verify the `keys/` exposure guard: `curl` `/videos/test/keys/ep-1.key` returns 404 while `/videos/test/ep-1/720p/media-720p.m3u8` returns 200.
