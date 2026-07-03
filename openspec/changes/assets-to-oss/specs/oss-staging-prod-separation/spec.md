## MODIFIED Requirements

### Requirement: staging vs prod path layout

OSS objects representing each drama's full asset set SHALL be stored under the prod prefix within the same bucket. The staging prefix MAY still exist for historical data and cleanup compatibility, but it is no longer part of the normal publish path.

**Per-rung media files (existing)**:
- `Drama/staging/{slug}/{ep_dir}/{ladder}/init-{ladder}.mp4`
- `Drama/staging/{slug}/{ep_dir}/{ladder}/seg-{ladder}-*.m4s`
- `Drama/prod/{slug}/{ep_dir}/{ladder}/init-{ladder}.mp4`
- `Drama/prod/{slug}/{ep_dir}/{ladder}/seg-{ladder}-*.m4s`

**Drama-level posters (NEW)**:
- `Drama/staging/{slug}/poster/{lang_code}.{ext}` (ext ∈ {jpg, png, webp})
- `Drama/prod/{slug}/poster/{lang_code}.{ext}`

**Per-episode covers (NEW)**:
- `Drama/staging/{slug}/{ep_dir}/cover.jpg`
- `Drama/prod/{slug}/{ep_dir}/cover.jpg`

**Per-episode WebVTT subtitles (NEW)**:
- `Drama/staging/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`
- `Drama/prod/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`

The encoder pipeline + admin handlers (this server) SHALL write current assets directly under `Drama/prod/`. Sync-time helpers (`publish_ladder_to_prod`, `publish_poster_to_prod`, `publish_cover_to_prod`, `publish_subtitle_to_prod`) SHALL generate prod object keys / playlist text and MUST NOT list/copy staging objects in the normal sync path.

The constants `OSS_STAGING_PREFIX` (= `"Drama/staging"`) and `OSS_PROD_PREFIX` (= `"Drama/prod"`) SHALL be defined in `app/oss_upload.py` and used by all callers; the strings SHALL NOT be re-hardcoded elsewhere.

#### Scenario: encoder writes media to prod
- **GIVEN** OSS mode enabled and an episode `(slug='ly', ep=3)` reaching pipeline completion
- **WHEN** worker runs `publish_ladder('ly', 'ep-3', '720p')`
- **THEN** the OSS object `Drama/prod/ly/ep-3/720p/init-720p.mp4` exists
- **AND** at least one `Drama/prod/ly/ep-3/720p/seg-720p-N.m4s` exists

#### Scenario: poster upload writes to prod
- **GIVEN** OSS mode enabled and an existing drama `ly` with `name` translation in `zh-rCN`
- **WHEN** the operator POSTs `image/jpeg` to `/admin/dramas/ly/poster?lang=zh-rCN`
- **THEN** the OSS object `Drama/prod/ly/poster/zh-rCN.jpg` exists with the uploaded bytes

#### Scenario: cover extraction writes to prod
- **GIVEN** OSS mode enabled and an episode upload reaching pipeline completion
- **WHEN** worker uploads the extracted cover via `upload_cover_to_staging('ly', 'ep-3')`
- **THEN** the OSS object `Drama/prod/ly/ep-3/cover.jpg` exists

#### Scenario: subtitle upload writes to prod
- **GIVEN** OSS mode enabled and an existing episode `ly-ep-3`
- **WHEN** the operator POSTs `text/vtt` to `/admin/episodes/ly/3/subtitles?lang=en`
- **THEN** the OSS object `Drama/prod/ly/ep-3/subtitles/en.vtt` exists with the uploaded bytes

### Requirement: unpublish primitives for prod and staging

`app/publish.py` SHALL expose:

- `unpublish_ladder_from_prod(slug, ep_dir, ladder) -> None`: deletes all objects under `Drama/prod/{slug}/{ep_dir}/{ladder}/`. Idempotent (no-op when nothing matches).
- `unpublish_episode_from_prod(slug, ep_dir) -> None` (NEW): deletes all objects under `Drama/prod/{slug}/{ep_dir}/`. Sweeps cover, subtitles, and all three ladder directories in one prefix sweep. Idempotent.
- `unpublish_drama_from_prod(slug) -> None`: deletes all objects under `Drama/prod/{slug}/`. Idempotent.
- `unpublish_episode_from_staging(slug, ep_dir) -> None`: deletes all objects under `Drama/staging/{slug}/{ep_dir}/`. Idempotent.
- `unpublish_drama_from_staging(slug) -> None`: deletes all objects under `Drama/staging/{slug}/`. Idempotent.
- `unpublish_poster_from_staging(slug, lang) -> None` (NEW): deletes every `Drama/staging/{slug}/poster/{lang}.*` regardless of extension. Idempotent.
- `unpublish_poster_from_prod(slug, lang) -> None` (NEW): deletes every `Drama/prod/{slug}/poster/{lang}.*` regardless of extension. Idempotent.
- `unpublish_subtitle_from_staging(slug, ep_dir, lang) -> None` (NEW): deletes `Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt`. Idempotent.
- `unpublish_subtitle_from_prod(slug, ep_dir, lang) -> None` (NEW): deletes `Drama/prod/{slug}/{ep_dir}/subtitles/{lang}.vtt`. Idempotent.

All helpers use `list_with_prefix` then `batch_delete`. Failures bubble as exceptions; callers decide whether to log + continue (delete handlers) or fail hard (sync handler).

The drama-level / episode-level prefix-sweep helpers (`unpublish_drama_*`, `unpublish_episode_*`) MUST NOT be replaced by per-asset partial-cleanup helpers — they are the correct primitive for whole-entity deletes and naturally cover assets added in this change without any modification.

#### Scenario: unpublish_ladder_from_prod removes only the targeted ladder
- **GIVEN** prod has objects under `Drama/prod/ly/ep-3/720p/` AND `Drama/prod/ly/ep-3/540p/`
- **WHEN** the application calls `unpublish_ladder_from_prod('ly', 'ep-3', '720p')`
- **THEN** all `Drama/prod/ly/ep-3/720p/...` keys are gone
- **AND** the `540p` keys remain

#### Scenario: unpublish_episode_from_prod sweeps cover + subtitles + all ladders
- **GIVEN** prod has objects under `Drama/prod/ly/ep-3/cover.jpg`, `Drama/prod/ly/ep-3/subtitles/en.vtt`, `Drama/prod/ly/ep-3/720p/init-720p.mp4`, `Drama/prod/ly/ep-3/540p/init-540p.mp4`
- **WHEN** the application calls `unpublish_episode_from_prod('ly', 'ep-3')`
- **THEN** zero objects remain under `Drama/prod/ly/ep-3/`
- **AND** any objects under `Drama/prod/ly/ep-4/` are unchanged

#### Scenario: unpublish_poster_from_staging deletes any extension
- **GIVEN** staging has `Drama/staging/ly/poster/zh-rCN.jpg` AND `Drama/staging/ly/poster/zh-rCN.png` (a stale file from a prior format)
- **WHEN** the application calls `unpublish_poster_from_staging('ly', 'zh-rCN')`
- **THEN** both objects are gone
- **AND** posters in other languages (e.g. `Drama/staging/ly/poster/en.jpg`) are unchanged

#### Scenario: unpublish_subtitle_from_staging deletes a single language
- **GIVEN** staging has `Drama/staging/ly/ep-3/subtitles/en.vtt` AND `Drama/staging/ly/ep-3/subtitles/zh-rCN.vtt`
- **WHEN** the application calls `unpublish_subtitle_from_staging('ly', 'ep-3', 'en')`
- **THEN** `Drama/staging/ly/ep-3/subtitles/en.vtt` is gone
- **AND** `Drama/staging/ly/ep-3/subtitles/zh-rCN.vtt` remains

#### Scenario: unpublish on missing prefix is a no-op
- **GIVEN** no objects under `Drama/prod/never/`
- **WHEN** the application calls `unpublish_drama_from_prod('never')`
- **THEN** no error is raised

## ADDED Requirements

### Requirement: staging-upload primitives for non-segment assets

`app/publish.py` SHALL expose four upload helpers that callers invoke after writing the asset to local disk:

- `upload_poster_to_staging(slug: str, lang: str, local_path: Path) -> str`: uploads `local_path` to `Drama/staging/{slug}/poster/{lang}.{ext}` (extension inferred from `local_path.suffix`). Returns the absolute staging URL. On OSS failure raises `PublishError`.
- `upload_cover_to_staging(slug: str, ep_dir: str, local_path: Path) -> str`: uploads `local_path` to `Drama/staging/{slug}/{ep_dir}/cover.jpg`. Returns staging URL.
- `upload_subtitle_to_staging(slug: str, ep_dir: str, lang: str, local_path: Path) -> str`: uploads `local_path` to `Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt`. Returns staging URL.

These primitives SHALL be called **after** the local-disk write succeeds and **before** the HTTP handler returns 2xx. If the OSS upload raises, the handler MUST roll back the local-disk write (unlink the file) and respond 500.

When `settings.oss_enabled` is `false`, these primitives MUST NOT be called; behavior of the upload handlers stays local-only.

#### Scenario: poster upload helper writes the right key
- **GIVEN** OSS mode enabled, drama `ly`, local file `OUT_DIR/ly/poster/zh-rCN.jpg`
- **WHEN** the application calls `upload_poster_to_staging('ly', 'zh-rCN', Path('.../zh-rCN.jpg'))`
- **THEN** OSS receives a `put_object_from_file` call with key `"Drama/staging/ly/poster/zh-rCN.jpg"`
- **AND** the function returns `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/ly/poster/zh-rCN.jpg"`

#### Scenario: cover upload helper writes the right key
- **GIVEN** OSS mode enabled, episode `ly-ep-3`
- **WHEN** the application calls `upload_cover_to_staging('ly', 'ep-3', Path('.../cover.jpg'))`
- **THEN** OSS receives a `put_object_from_file` call with key `"Drama/staging/ly/ep-3/cover.jpg"`
- **AND** the function returns `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/ly/ep-3/cover.jpg"`

#### Scenario: subtitle upload helper writes the right key
- **GIVEN** OSS mode enabled, episode `ly-ep-3`, lang `en`
- **WHEN** the application calls `upload_subtitle_to_staging('ly', 'ep-3', 'en', Path('.../en.vtt'))`
- **THEN** OSS receives a `put_object_from_file` call with key `"Drama/staging/ly/ep-3/subtitles/en.vtt"`
- **AND** the function returns `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/ly/ep-3/subtitles/en.vtt"`

#### Scenario: upload helper failure rolls back local file (handler responsibility)
- **GIVEN** OSS mode enabled and the OSS service is temporarily unreachable
- **WHEN** the operator POSTs a poster and `upload_poster_to_staging` raises `PublishError`
- **THEN** the handler MUST unlink the just-written local file
- **AND** respond HTTP 500 with the error message
- **AND** the DB translation row MUST NOT be updated to point at the missing file

### Requirement: prod-publish primitives for non-segment assets

`app/publish.py` SHALL expose three publish helpers that return the prod object key for the current asset:

- `publish_poster_to_prod(slug: str, lang: str, ext: str) -> str`: returns `Drama/prod/{slug}/poster/{lang}.{ext}`.
- `publish_cover_to_prod(slug: str, ep_dir: str) -> str`: returns `Drama/prod/{slug}/{ep_dir}/cover.jpg`.
- `publish_subtitle_to_prod(slug: str, ep_dir: str, lang: str) -> str`: returns `Drama/prod/{slug}/{ep_dir}/subtitles/{lang}.vtt`.

These mirror `publish_ladder_to_prod` semantically after migration: called by the sync worker, but no remote `list_with_prefix` / `copy_object` is performed.

#### Scenario: poster publish-to-prod returns prod key
- **GIVEN** drama `ly` has a `zh-rCN` poster filename `zh-rCN.jpg`
- **WHEN** the sync worker calls `publish_poster_to_prod('ly', 'zh-rCN', 'jpg')`
- **THEN** the function returns `"Drama/prod/ly/poster/zh-rCN.jpg"`

#### Scenario: cover publish-to-prod returns prod key
- **GIVEN** episode `ly` / `ep-3` is ready
- **WHEN** the sync worker calls `publish_cover_to_prod('ly', 'ep-3')`
- **THEN** the function returns `"Drama/prod/ly/ep-3/cover.jpg"`

#### Scenario: subtitle publish-to-prod returns prod key
- **GIVEN** episode `ly` / `ep-3` has an `en` subtitle row
- **WHEN** the sync worker calls `publish_subtitle_to_prod('ly', 'ep-3', 'en')`
- **THEN** the function returns `"Drama/prod/ly/ep-3/subtitles/en.vtt"`
