## 1. OSS module — constants and primitives

- [x] 1.1 In `app/oss_upload.py`, add module-level constants: `OSS_STAGING_PREFIX = f"{ossBaseDir}/staging"`, `OSS_PROD_PREFIX = f"{ossBaseDir}/prod"`, `oss_staging_public_base_url = f"{oss_public_base_url}/staging"`, `oss_prod_public_base_url = f"{oss_public_base_url}/prod"`. Keep `ossBaseDir`, `oss_public_base_url`, `auth`, `bucket`, `upload_file` as-is.
- [x] 1.2 Add `def copy_object(src_key: str, dst_key: str) -> None`: server-side copy via `bucket.copy_object(bucket.bucket_name, src_key, dst_key)`. Raise on failure (oss2 raises on non-2xx automatically; surface clearly).
- [x] 1.3 Add `def list_with_prefix(prefix: str) -> list[str]`: paginates with `bucket.list_objects(prefix=..., marker=..., max_keys=1000)` until `is_truncated` is false. Returns list of `key` strings.
- [x] 1.4 Add `def batch_delete(keys: list[str]) -> None`: chunks into 1000-key batches, calls `bucket.batch_delete_objects(chunk)` for each. No-op on empty list.

## 2. publish.py — staging path + new prod / cleanup primitives

- [x] 2.1 Modify `publish_ladder` to use the new staging prefix: `remote_dir = f"{OSS_STAGING_PREFIX}/{slug}/{ep_dir}/{ladder}"`. Modify the m3u8 rewrite call to use `oss_staging_public_base_url`: `rewrite_playlist(text, f"{oss_staging_public_base_url}/{slug}/{ep_dir}/{ladder}")`.
- [x] 2.2 Add `def publish_ladder_to_prod(slug, ep_dir, ladder) -> str` per the spec. List `Drama/staging/{slug}/{ep_dir}/{ladder}/`, raise `PublishError` if empty. Loop and `copy_object` each `.mp4` / `.m4s` to the prod path. Read local m3u8, return text with `oss_staging_public_base_url + "/"` replaced by `oss_prod_public_base_url + "/"`.
- [x] 2.3 Add `unpublish_ladder_from_prod(slug, ep_dir, ladder) -> None`: list + batch_delete under prod ladder dir.
- [x] 2.4 Add `unpublish_drama_from_prod(slug) -> None`: list + batch_delete under `Drama/prod/{slug}/`.
- [x] 2.5 Add `unpublish_episode_from_staging(slug, ep_dir) -> None`: list + batch_delete under `Drama/staging/{slug}/{ep_dir}/`.
- [x] 2.6 Add `unpublish_drama_from_staging(slug) -> None`: list + batch_delete under `Drama/staging/{slug}/`.
- [x] 2.7 All four unpublish helpers tolerate empty results without error.

## 3. API router — staging URL in EpisodeInfo

- [x] 3.1 In `app/routers/api.py`, change the OSS-mode branch of `_row_to_episode_info` from `oss_public_base_url` to `oss_staging_public_base_url`. Verify import.
- [x] 3.2 Confirm no other code path references `oss_public_base_url` outside `oss_upload.py` after this change.

## 4. Delete handlers — staging OSS cleanup

- [x] 4.1 In `app/routers/admin.py` `admin_delete_episode`: when `settings.oss_enabled`, after the local cleanup block (rmtree + key file unlinks), call `await asyncio.to_thread(publish.unpublish_episode_from_staging, drama_slug, ep_dir_name)`. Catch exceptions; append to `warnings` rather than raising.
- [x] 4.2 In `admin_delete_drama` (added in `drama-as-entity`): same pattern — `unpublish_drama_from_staging(slug)` after local rmtree, with warnings collection.
- [x] 4.3 The handlers pass through OSS-disabled cases unchanged.

## 5. Migration helper

- [x] 5.1 Update `scripts/migrate_to_oss.py` (existing) — or add a new sibling script `scripts/migrate_to_staging_prefix.py`. The new one: for each `status=ready` row, for each ladder, re-call `publish_ladder(slug, ep_dir, ladder)` (which now writes to staging prefix and rewrites the local m3u8). Idempotent — running twice on already-migrated data is a no-op.
- [x] 5.2 The script logs each upload + rewrite with INFO; logs candidate-for-cleanup keys under the legacy `Drama/{slug}/...` path WITHOUT deleting them.
- [x] 5.3 Document the script's usage in CLAUDE.md alongside the existing `migrate_to_oss.py` reference.

## 6. Documentation

- [x] 6.1 Update `CLAUDE.md`'s "OSS 双 host 拓扑" section: the OSS bucket now has two prefixes (staging + prod). Update the example m3u8 to use `Drama/staging/...` URLs. Add a note that prod URLs only appear in the business server's m3u8 (produced by `publish_ladder_to_prod`).
- [x] 6.2 Update CLAUDE.md's URL归属表: `init.mp4` and `seg-*.m4s` rows now read "OSS staging prefix (this server) / OSS prod prefix (business server)".
- [x] 6.3 Add a new subsection "Manual sync — staging→prod copy primitives" listing the four unpublish helpers + `publish_ladder_to_prod`, marked as "consumed by step 6's `business-server-sync` change."
- [x] 6.4 Update the Migration sub-section: there are now two migration scripts (legacy → staging via `migrate_to_staging_prefix.py`; if upgrading from a single-tenant deploy with data).

## 7. Manual verification

- [ ] 7.1 Start fresh (delete `hls.db`, delete OSS objects under `Drama/`). With `OSS_ENABLED=true`, upload one episode end-to-end. Verify OSS console shows objects only under `Drama/staging/{slug}/...`; nothing under `Drama/prod/`.
- [ ] 7.2 GET `/api/episodes/{slug}/{ep}` — verify `initUrl` / `firstSegUrl` reference `Drama/staging/`. Validate against `episode-info-schema.json`.
- [ ] 7.3 Open the local m3u8 in `out/{slug}/ep-{n}/720p/media-720p.m3u8` — verify init / segment URLs reference `Drama/staging/`. `#EXT-X-KEY:URI` is `/drm/...`.
- [ ] 7.4 In a Python REPL, call `publish.publish_ladder_to_prod(slug, "ep-1", "720p")`. Verify (a) OSS now has objects under `Drama/prod/{slug}/ep-1/720p/`, (b) the returned string has `Drama/prod/...` in init/segment lines, `Drama/staging` substring is absent, `#EXT-X-KEY` line unchanged.
- [ ] 7.5 Call `publish.publish_ladder_to_prod` again — verify OSS objects are overwritten (modification time is newer) and the returned string is byte-equal to the first call.
- [ ] 7.6 Call `publish.unpublish_ladder_from_prod(slug, "ep-1", "720p")` — verify the prod objects are gone; staging unchanged.
- [ ] 7.7 DELETE the episode via `/admin/episodes/{slug}/{ep}` — verify staging OSS objects under `Drama/staging/{slug}/ep-1/` are removed (response 200, empty `warnings`).
- [ ] 7.8 Re-upload, then DELETE the drama via `/admin/dramas/{slug}` — verify all staging OSS objects under `Drama/staging/{slug}/` are removed.
- [ ] 7.9 `unpublish_drama_from_prod` with no objects under that prefix — verify no error.
- [ ] 7.10 `list_with_prefix` against a prefix containing >1000 objects (synthesize via repeated upload if needed) — verify all keys returned.

## 8. Spec sync

- [x] 8.1 `openspec validate oss-staging-prod-separation --strict`.
- [x] 8.2 If prior changes have been archived, re-validate to ensure MODIFIED `oss-segment-hosting` references resolve cleanly.
