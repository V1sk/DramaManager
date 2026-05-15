## ADDED Requirements

### Requirement: staging vs prod path layout

OSS objects representing each rung's media files SHALL be stored under two parallel prefixes within the same bucket:
- `Drama/staging/{slug}/{ep_dir}/{ladder}/init-{ladder}.mp4`
- `Drama/staging/{slug}/{ep_dir}/{ladder}/seg-{ladder}-*.m4s`
- `Drama/prod/{slug}/{ep_dir}/{ladder}/init-{ladder}.mp4`
- `Drama/prod/{slug}/{ep_dir}/{ladder}/seg-{ladder}-*.m4s`

The encoder pipeline (this server) SHALL only ever write under `Drama/staging/`. The `Drama/prod/` subtree is populated exclusively by sync-time copy operations (`publish_ladder_to_prod`).

The constants `OSS_STAGING_PREFIX` (= `"Drama/staging"`) and `OSS_PROD_PREFIX` (= `"Drama/prod"`) SHALL be defined in `app/oss_upload.py` and used by all callers; the strings SHALL NOT be re-hardcoded elsewhere.

#### Scenario: encoder writes to staging only
- **GIVEN** OSS mode enabled and an episode `(slug='ly', ep=3)` reaching pipeline completion
- **WHEN** worker runs `publish_ladder('ly', 'ep-3', '720p')`
- **THEN** the OSS object `Drama/staging/ly/ep-3/720p/init-720p.mp4` exists
- **AND** at least one `Drama/staging/ly/ep-3/720p/seg-720p-N.m4s` exists
- **AND** no objects under `Drama/prod/ly/...` were created by this call

### Requirement: public URL constants for staging and prod

`app/oss_upload.py` SHALL expose two module-level constants:
- `oss_staging_public_base_url` = `f"{oss_public_base_url}/staging"` = `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging"`
- `oss_prod_public_base_url` = `f"{oss_public_base_url}/prod"` = `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod"`

Callers SHALL import the env-specific constant (`oss_staging_public_base_url` for code running on this staging server, `oss_prod_public_base_url` for code that produces prod-flavored URLs to be shipped elsewhere). The bucket-level `oss_public_base_url` SHALL NOT be used directly to construct absolute OSS URLs in any module other than `oss_upload.py` itself.

#### Scenario: constants are exported with the expected values
- **WHEN** `app.oss_upload` is imported
- **THEN** `app.oss_upload.oss_staging_public_base_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging"`
- **AND** `app.oss_upload.oss_prod_public_base_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod"`

### Requirement: OSS primitives — copy_object, list_with_prefix, batch_delete

`app/oss_upload.py` SHALL expose three module-level functions:

- `copy_object(src_key: str, dst_key: str) -> None`: server-side copy within the bucket. Raises on failure (oss2 exception or non-2xx response). Both keys MUST NOT begin with `/`.
- `list_with_prefix(prefix: str) -> list[str]`: returns all object keys under the prefix, paginated internally (1000-per-page until no more `is_truncated`). Empty result returns `[]`.
- `batch_delete(keys: list[str]) -> None`: bulk-deletes the given keys, chunking into batches of 1000 per OSS call. No-op on empty input. Tolerates already-deleted objects.

These primitives SHALL be used only by `app/publish.py`; routers and other modules SHALL NOT call OSS directly.

#### Scenario: copy_object copies a single key
- **GIVEN** `Drama/staging/ly/ep-3/720p/init-720p.mp4` exists
- **WHEN** the application calls `copy_object("Drama/staging/ly/ep-3/720p/init-720p.mp4", "Drama/prod/ly/ep-3/720p/init-720p.mp4")`
- **THEN** the destination object exists with byte-identical content
- **AND** the source object is unchanged

#### Scenario: list_with_prefix paginates transparently
- **GIVEN** an OSS prefix containing 1500 objects
- **WHEN** the application calls `list_with_prefix(prefix)`
- **THEN** the returned list contains all 1500 keys

#### Scenario: batch_delete chunks at 1000
- **GIVEN** a list of 2500 keys
- **WHEN** the application calls `batch_delete(keys)`
- **THEN** OSS receives 3 batch-delete calls (1000 + 1000 + 500)
- **AND** all 2500 keys are removed

#### Scenario: batch_delete tolerates empty input
- **WHEN** the application calls `batch_delete([])`
- **THEN** no OSS calls are made
- **AND** the function returns without error

### Requirement: publish_ladder_to_prod copies and returns prod-flavored m3u8

`app/publish.py` SHALL expose `publish_ladder_to_prod(slug: str, ep_dir: str, ladder: str) -> str` that:

1. Lists every key under `Drama/staging/{slug}/{ep_dir}/{ladder}/`. If the list is empty, raises `PublishError` with a clear message ("no staging objects; publish_ladder must run first").
2. For every key in that list ending with `.mp4` or `.m4s`, copies it to the corresponding `Drama/prod/{slug}/{ep_dir}/{ladder}/` location via `copy_object`. Other extensions are skipped (defensive — should not exist).
3. Reads the local file at `OUT_DIR/{slug}/{ep_dir}/{ladder}/media-{ladder}.m3u8`. If missing, raises `PublishError`.
4. Returns the local m3u8 text with `oss_staging_public_base_url + "/"` replaced by `oss_prod_public_base_url + "/"` everywhere it occurs. The `#EXT-X-KEY:URI` line (which contains a `/drm/...` relative path) is unaffected.

The function is idempotent: re-runs overwrite prod objects with the (likely identical) staging copies and re-derive the same m3u8 text.

#### Scenario: full ladder publish to prod returns rewritten m3u8
- **GIVEN** staging has `Drama/staging/ly/ep-3/720p/init-720p.mp4` and 60 `seg-720p-N.m4s` objects, and the local m3u8 references `https://photobundle.../Drama/staging/ly/ep-3/720p/...` URLs
- **WHEN** the application calls `publish_ladder_to_prod('ly', 'ep-3', '720p')`
- **THEN** all 61 objects exist under `Drama/prod/ly/ep-3/720p/...`
- **AND** the function returns a string where every occurrence of `https://photobundle.../Drama/staging/ly/ep-3/720p/` has been replaced by `https://photobundle.../Drama/prod/ly/ep-3/720p/`
- **AND** the line `#EXT-X-KEY:METHOD=AES-128,URI="/drm/ly/ep-3/key",IV=0x...` is byte-identical to the local m3u8

#### Scenario: prod m3u8 still passes idempotent rewrite
- **GIVEN** the prod m3u8 returned from a successful call
- **WHEN** `publish_ladder_to_prod` runs a second time for the same `(slug, ep_dir, ladder)`
- **THEN** the returned text is byte-equal to the previous return value
- **AND** the prod objects are overwritten in place (same content)

#### Scenario: staging absent → PublishError
- **GIVEN** no objects under `Drama/staging/never/ep-1/720p/`
- **WHEN** the application calls `publish_ladder_to_prod('never', 'ep-1', '720p')`
- **THEN** the function raises `PublishError`
- **AND** no objects are created under `Drama/prod/never/`

### Requirement: unpublish primitives for prod and staging

`app/publish.py` SHALL expose:

- `unpublish_ladder_from_prod(slug, ep_dir, ladder) -> None`: deletes all objects under `Drama/prod/{slug}/{ep_dir}/{ladder}/`. Idempotent (no-op when nothing matches).
- `unpublish_drama_from_prod(slug) -> None`: deletes all objects under `Drama/prod/{slug}/`. Idempotent.
- `unpublish_episode_from_staging(slug, ep_dir) -> None`: deletes all objects under `Drama/staging/{slug}/{ep_dir}/`. Idempotent.
- `unpublish_drama_from_staging(slug) -> None`: deletes all objects under `Drama/staging/{slug}/`. Idempotent.

All four use `list_with_prefix` then `batch_delete`. Failures bubble as exceptions; callers decide whether to log + continue (delete handlers) or fail hard (sync handler in step 6).

#### Scenario: unpublish_ladder_from_prod removes only the targeted ladder
- **GIVEN** prod has objects under `Drama/prod/ly/ep-3/720p/` AND `Drama/prod/ly/ep-3/540p/`
- **WHEN** the application calls `unpublish_ladder_from_prod('ly', 'ep-3', '720p')`
- **THEN** all `Drama/prod/ly/ep-3/720p/...` keys are gone
- **AND** the `540p` keys remain

#### Scenario: unpublish on missing prefix is a no-op
- **GIVEN** no objects under `Drama/prod/never/`
- **WHEN** the application calls `unpublish_drama_from_prod('never')`
- **THEN** no error is raised
- **AND** no OSS list-and-delete cycle leaves residue

### Requirement: staging-side OSS cleanup on local delete

The episode-deletion and drama-deletion handlers SHALL clean up the corresponding `Drama/staging/...` OSS objects whenever `settings.oss_enabled` is true, ensuring that local-state and staging-OSS-state stay in lockstep on this server. When `settings.oss_enabled` is false, no OSS cleanup MUST be attempted. Prod-side cleanup MUST NOT be performed by these handlers; clearing `Drama/prod/...` is exclusively the responsibility of the manual sync flow added by `business-server-sync` (step 6).

Concretely:
- The episode-deletion handler (`DELETE /admin/episodes/{slug}/{ep}`) MUST, after successful local file cleanup, call `unpublish_episode_from_staging(slug, ep_dir)` where `ep_dir = "ep-{ep_number}"`. OSS failures MUST NOT roll back the DB delete; instead an entry SHALL be added to the response body's `warnings` array describing the OSS failure.
- The drama-deletion handler (`DELETE /admin/dramas/{slug}`, added in `drama-as-entity`) MUST, after successful local file cleanup, call `unpublish_drama_from_staging(slug)` with the same warnings semantics.

#### Scenario: episode delete cleans staging OSS
- **GIVEN** OSS mode enabled, episode `ly-ep-3` exists locally and at `Drama/staging/ly/ep-3/...` in OSS
- **WHEN** the operator calls `DELETE /admin/episodes/ly/3`
- **THEN** the response is 200 with an empty `warnings` array
- **AND** zero objects remain under `Drama/staging/ly/ep-3/`
- **AND** any objects under `Drama/prod/ly/ep-3/` are unchanged (cleanup deferred to sync)

#### Scenario: episode delete tolerates OSS failure
- **GIVEN** OSS mode enabled and the OSS service is temporarily unreachable
- **WHEN** the operator calls `DELETE /admin/episodes/ly/3`
- **THEN** the local files are removed
- **AND** the DB row is deleted
- **AND** the response includes a warning naming the OSS failure

#### Scenario: drama delete cleans staging OSS for whole drama
- **GIVEN** OSS mode enabled, drama `ly` has zero episodes (operator already deleted them) but `Drama/staging/ly/poster/...` objects remain (if poster sync ever moved posters to OSS — currently posters stay local, but as a defensive cleanup)
- **WHEN** the operator calls `DELETE /admin/dramas/ly`
- **THEN** zero objects remain under `Drama/staging/ly/`
