## 1. OSS module — extension

- [x] 1.1 Confirm `OSS_STAGING_PREFIX` and `OSS_PROD_PREFIX` constants in `app/oss_upload.py` are usable for non-segment paths (no change expected; just verify import sites).

## 2. publish.py — staging upload helpers

- [x] 2.1 Add `upload_poster_to_staging(slug, lang, local_path) -> str` in `app/publish.py`. Determines extension from `local_path.suffix` (`.jpg` / `.png` / `.webp`). Calls `oss_upload.upload_file(...)`. Returns absolute staging URL (`{oss_staging_public_base_url}/{slug}/poster/{lang}.{ext}`). Raises `PublishError` on OSS failure.
- [x] 2.2 Add `upload_cover_to_staging(slug, ep_dir, local_path) -> str`. Always `.jpg`. Returns staging URL.
- [x] 2.3 Add `upload_subtitle_to_staging(slug, ep_dir, lang, local_path) -> str`. Always `.vtt`. Returns staging URL.
- [x] 2.4 Each helper: when `settings.oss_enabled` is False, calling it is a programmer error — raise `RuntimeError`. Callers gate on `settings.oss_enabled` themselves.

## 3. publish.py — prod copy helpers

- [x] 3.1 Add `publish_poster_to_prod(slug, lang, ext) -> str`. Calls `oss_upload.copy_object` from `Drama/staging/{slug}/poster/{lang}.{ext}` → prod sibling. Returns prod URL. Raises `PublishError` if staging object is missing.
- [x] 3.2 Add `publish_cover_to_prod(slug, ep_dir) -> str`. Same shape, fixed `cover.jpg`.
- [x] 3.3 Add `publish_subtitle_to_prod(slug, ep_dir, lang) -> str`. Same shape, `.vtt`.

## 4. publish.py — partial unpublish helpers

- [x] 4.1 Add `unpublish_poster_from_staging(slug, lang) -> None`. Lists `Drama/staging/{slug}/poster/{lang}.*` (any extension) via `list_with_prefix`, then `batch_delete`. Idempotent (empty result is a no-op).
- [x] 4.2 Add `unpublish_poster_from_prod(slug, lang) -> None`. Same shape, prod side.
- [x] 4.3 Add `unpublish_subtitle_from_staging(slug, ep_dir, lang) -> None`. Lists exact key `Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt`, deletes if present.
- [x] 4.4 Add `unpublish_subtitle_from_prod(slug, ep_dir, lang) -> None`. Same shape, prod side.

## 5. publish.py — episode-level prod unpublish

- [x] 5.1 Add `unpublish_episode_from_prod(slug, ep_dir) -> None`. Lists `Drama/prod/{slug}/{ep_dir}/` and `batch_delete`. Replaces the existing 3-call `unpublish_ladder_from_prod` loop on episode delete-sync (sweeps all ladders + cover + subtitles in one call).

## 6. Wire poster upload to OSS

- [x] 6.1 In `app/routers/admin.py` `admin_upload_drama_poster`: after the local file write succeeds and before the DB upsert, when `settings.oss_enabled`, call `publish.unpublish_poster_from_staging(slug, lang)` (clear prior extension) then `publish.upload_poster_to_staging(slug, lang, target_path)`. On `PublishError`, unlink the just-written local file and raise HTTPException 500.
- [x] 6.2 In `admin_delete_drama_poster`: after the DB row delete and local file delete, when `settings.oss_enabled`, call `publish.unpublish_poster_from_staging(slug, lang)` wrapped in try/except. On failure, log WARNING and continue (DB / disk delete already succeeded; OSS is best-effort).

## 7. Wire cover upload to OSS

- [x] 7.1 In `app/routers/admin.py` `_process_episode_upload` (shared by auto-increment and re-upload): after `extract_first_frame` succeeds, when `settings.oss_enabled`, call `publish.upload_cover_to_staging(drama_slug, ep_dir_name, cover_path)`. On `PublishError`, unlink `cover_path` and raise HTTPException 500 (same posture as ffprobe failure).
- [x] 7.2 In `app/routers/admin.py` `admin_upload_next_episode` (auto-increment): same wiring after its inline `extract_first_frame` call. On OSS failure, unlink `cover_path` AND `tmp_path`, raise 500.
- [x] 7.3 In `app/routers/api.py` `replace_cover`: after `shutil.copyfileobj` succeeds, when `settings.oss_enabled`, call `publish.upload_cover_to_staging(drama_slug, ep_dir, cover_path)`. On `PublishError`, attempt to restore prior cover (best-effort; if a `.bak` snapshot was made before overwrite) or unlink the file; raise HTTPException 500. Snapshot strategy: copy `cover.jpg` to `cover.jpg.bak` before overwrite; on success delete `.bak`; on failure restore from `.bak`.

## 8. Wire subtitle upload to OSS

- [x] 8.1 In `app/routers/admin.py` `admin_upload_subtitle`: after `target_path.write_bytes(body)` succeeds and before `db.upsert_subtitle`, when `settings.oss_enabled`, call `publish.upload_subtitle_to_staging(drama_slug, ep_dir, lang, target_path)`. On `PublishError`, unlink `target_path` and raise HTTPException 500.
- [x] 8.2 In `admin_delete_subtitle`: after the DB row delete and local file unlink, when `settings.oss_enabled`, call `publish.unpublish_subtitle_from_staging(drama_slug, ep_dir, lang)` wrapped in try/except. Log WARNING on failure; continue.

## 9. Sync worker — drama path

- [x] 9.1 In `app/sync.py` `handle_drama_sync` (the upsert branch): for each `(lang, fields)` in `db.list_drama_translations(slug)` where `fields["poster"]` is non-null, parse the extension from the URL (`/videos/{slug}/poster/{lang}.{ext}` → `ext`). Call `await asyncio.to_thread(publish.publish_poster_to_prod, slug, lang, ext)`. Collect into a `{lang: prod_url}` dict. Pass into `build_drama_payload` as a new parameter.
- [x] 9.2 Refactor `build_drama_payload(slug)` → `build_drama_payload(slug, *, poster_prod_urls: dict[str, str | None])`. The payload's `translations[lang].poster_url` field SHALL be `poster_prod_urls.get(lang)` (None if not in dict). Default arg is `{}` for callers that don't have OSS context (only the worker path builds prod URLs).
- [x] 9.3 If any `publish_poster_to_prod` raises `PublishError`, `handle_drama_sync` SHALL set the row `sync_failed` with the error and SHALL NOT proceed to `call_business`.

## 10. Sync worker — episode path

- [x] 10.1 In `handle_episode_sync` (the upsert branch): after the ladder publish loop, call `await asyncio.to_thread(publish.publish_cover_to_prod, slug, ep_dir)`. Collect the prod URL.
- [x] 10.2 For each subtitle row from `db.list_subtitles_for_slug_ep(slug, ep_number)`, call `await asyncio.to_thread(publish.publish_subtitle_to_prod, slug, ep_dir, lang)`. Collect into a list of `{lang_code, label, url}` dicts (label from the existing row).
- [x] 10.3 Refactor `build_episode_payload(slug, ep, playlists)` → `build_episode_payload(slug, ep, playlists, *, cover_prod_url: str, subtitles_prod: list[dict])`. The payload's `cover_url` is `cover_prod_url`; `subtitles[].url` are the prod URLs.
- [x] 10.4 If `publish_cover_to_prod` or any `publish_subtitle_to_prod` raises `PublishError`, set `sync_failed` and skip the business HTTP call.

## 11. Sync worker — episode delete path

- [x] 11.1 In `_execute_episode_delete_sync`: replace the `for ladder in (540p, 720p, 1080p): await asyncio.to_thread(publish.unpublish_ladder_from_prod, ...)` loop with a single `await asyncio.to_thread(publish.unpublish_episode_from_prod, slug, ep_dir)` call. This sweeps everything under `Drama/prod/{slug}/{ep_dir}/` (cover + subtitles + all ladders).

## 12. Pipeline / cover extraction OSS upload

- [x] 12.1 The `_process_episode_upload` helper (shared) and `admin_upload_next_episode` (auto-increment) both run cover extraction inline before responding. Per task 7.1 / 7.2 the cover upload to OSS staging happens in the request handler. Worker (`app/queue.py`) does NOT need its own cover upload — covers are already in OSS by the time the worker picks up the job.
- [x] 12.2 Audit: confirm worker (`app/queue.py`) does not extract or upload covers; the cover URL persisted in the DB row already points at the staging artifact.

## 13. Backfill script

- [x] 13.1 Create `scripts/backfill_assets_to_oss.py`: iterates DB rows; for each `dramas` row, walks `OUT_DIR/{slug}/poster/`, calls `upload_poster_to_staging` per file (skipping files whose `(slug, lang)` has no `(drama, slug, lang, poster, ...)` translation row); for each `episodes` row, uploads `OUT_DIR/{slug}/{ep_dir}/cover.jpg` if present, and walks `OUT_DIR/{slug}/{ep_dir}/subtitles/` uploading each `.vtt` whose `(episode_id, lang)` has a `subtitles` row.
- [x] 13.2 Script SHALL be idempotent (OSS PUT is overwrite). Refuse to run if `settings.oss_enabled` is false.
- [x] 13.3 Script SHALL log INFO per upload + summary (count of posters / covers / subtitles uploaded, count skipped because of missing DB row).

## 14. Documentation

- [x] 14.1 Update `docs/business-server-integration.md`:
  - Delete §5 "URL 拉取 contract" entirely.
  - In §4.1 (`POST /sync/dramas` body table), change `translations[lang].poster_url` description from "相对路径" to "**绝对 prod OSS URL**，业务服务器只记录不拉取".
  - In §4.3 (`POST /sync/episodes` body table), same edit for `cover_url` and `subtitles[].url`.
  - In §4.1 / §4.3 处理流程: remove the "同步拉取 ... 字节" step; remove 502 from response code tables.
  - In §10.2 (业务服务器侧 env vars table), remove `HLS_STAGING_HOST` (no longer needed).
  - In §11 (错误码总结) remove the 502 row.
  - In §12 (端到端示例时序), remove the "GET poster_url / GET cover_url / GET subtitle_url" arrows.
  - Bump version to v2.0 in §13 (协议变更历史) with **BREAKING CHANGE** note.
- [x] 14.2 Update `CLAUDE.md` "OSS 双 host 拓扑" section: extend the OSS bucket layout block to show the new poster / cover / subtitle paths under `Drama/staging/` and `Drama/prod/`. Update the URL 归属表 to mark cover / poster / subtitle as "OSS staging 前缀（这台服务器写）／ OSS prod 前缀（业务服务器读）".
- [x] 14.3 Update `CLAUDE.md` "Manual sync — staging→prod copy primitives" subsection to list the three new `publish_*_to_prod` helpers.

## 15. Manual verification

- [ ] 15.1 Fresh deploy: with `OSS_ENABLED=true`, upload a drama + poster + episode + subtitle. Inspect OSS console: `Drama/staging/{slug}/poster/{lang}.jpg`, `Drama/staging/{slug}/{ep_dir}/cover.jpg`, `Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt` all present.
- [ ] 15.2 Click "[同步整部剧]" with a mock business server (returns 200). Verify `Drama/prod/{slug}/poster/{lang}.jpg`, `Drama/prod/{slug}/{ep_dir}/cover.jpg`, `Drama/prod/{slug}/{ep_dir}/subtitles/{lang}.vtt` all populated. Inspect mock server's stored payload — `poster_url`, `cover_url`, `subtitles[].url` are absolute `Drama/prod/...` URLs.
- [ ] 15.3 Mock business server records URLs; verify it makes **zero** outbound HTTP GETs to fetch any of those URLs.
- [ ] 15.4 Replace cover (`POST /api/episodes/.../cover` with new image). Verify staging OSS `cover.jpg` updated; sync again; prod OSS updated.
- [ ] 15.5 Replace poster (re-POST with different format e.g. `.png` after `.jpg`). Verify staging OSS has new `.png`, no stale `.jpg`. Sync; prod OSS has `.png`.
- [ ] 15.6 Delete one subtitle. Verify staging OSS `.vtt` for that lang gone; other langs intact. Sync; prod OSS reflects.
- [ ] 15.7 Delete an episode (synced before). Sync. Verify `Drama/prod/{slug}/{ep_dir}/` is fully empty (single `unpublish_episode_from_prod` sweep).
- [ ] 15.8 Delete a drama (synced before). Sync. Verify `Drama/prod/{slug}/` is fully empty (`unpublish_drama_from_prod` sweep).
- [ ] 15.9 Backfill: stand up a deploy with pre-existing local dramas/episodes (no OSS for posters/covers/subtitles yet). Run `scripts/backfill_assets_to_oss.py`. Verify all expected OSS staging objects appear; idempotent re-run is a no-op (overwrite, no error).
- [ ] 15.10 OSS-disabled deploy: confirm none of the upload handlers attempt OSS calls; admin UX unchanged from pre-change.

## 16. Spec sync

- [x] 16.1 `openspec validate assets-to-oss --strict` passes.
- [x] 16.2 If prior changes have been archived, re-validate to ensure MODIFIED references resolve.
