## MODIFIED Requirements

### Requirement: sync worker behavior

The service SHALL run a single background coroutine `sync_worker_loop` consuming a module-level `asyncio.Queue` (`sync_queue`). Job types: `SyncDramaJob(slug)` and `SyncEpisodeJob(slug, ep_number)`.

For `SyncDramaJob`:
1. Read drama row.
2. If `sync_status='pending_delete'`: call `DELETE /sync/dramas/{slug}` against the business server; on 2xx, call `unpublish_drama_from_prod(slug)` (prefix sweep — covers all assets including posters), then physically delete the drama row from the local DB. On non-2xx, set `sync_status='sync_failed'` with the error.
3. Otherwise (status=`syncing`):
   a. For each `(lang, ext)` poster present in this drama's translations, call `publish.publish_poster_to_prod(slug, lang, ext)`. Collect the returned prod URLs.
   b. Build the `POST /sync/dramas` payload (drama row + translations + tags inline + actors inline + languages-used-by-drama inline). The `translations[lang].poster_url` field SHALL be the absolute prod OSS URL returned by step 3a (not a relative path). Languages without a poster keep `poster_url=null`.
   c. Call the business server. On 2xx, set `sync_status='clean'`, refresh `last_synced_at`. On non-2xx, set `sync_status='sync_failed'` with the error and stop (do not enqueue child episodes).
4. After a successful drama upsert, enqueue a `SyncEpisodeJob` for every episode of this drama whose `sync_status ∈ {dirty, pending_delete}`.

For `SyncEpisodeJob`:
1. Read episode row + drama row.
2. If `dramas.{slug}.last_synced_at IS NULL`: set `sync_status='sync_failed'` with `sync_error='drama not synced'`. Stop.
3. If episode's `sync_status='pending_delete'`: call `DELETE /sync/episodes/{slug}/{ep}`; on 2xx, call `unpublish_episode_from_prod(slug, "ep-{ep}")` (single prefix sweep covering all ladders, cover, subtitles), then physically delete the episode row.
4. Otherwise:
   a. Call `publish_ladder_to_prod` for each ladder (collect prod-flavored m3u8 strings).
   b. Call `publish.publish_cover_to_prod(slug, "ep-{ep}")` to copy the cover staging→prod. Collect the returned prod URL.
   c. For each subtitle row, call `publish.publish_subtitle_to_prod(slug, "ep-{ep}", lang)`. Collect the returned prod URLs.
   d. Build the `POST /sync/episodes` payload. `cover_url` SHALL be the absolute prod URL from step 4b. Each `subtitles[].url` SHALL be the absolute prod URL from step 4c. Each `playlists[ladder]` is the prod m3u8 text from 4a (already references prod OSS URLs).
   e. Call the business server. On 2xx, set `sync_status='clean'`, refresh `last_synced_at`. On non-2xx, set `sync_status='sync_failed'`.

If any `publish_*_to_prod` call raises `PublishError` (e.g. staging object missing because the operator deleted it locally without re-uploading), the worker SHALL set `sync_status='sync_failed'` with a descriptive error and SHALL NOT call the business server.

The worker SHALL handle exceptions per-job — a failure in one job MUST NOT crash the worker. The worker MUST mark the job's row `sync_failed` with a useful error before continuing.

#### Scenario: drama sync copies posters to prod before HTTP call
- **GIVEN** drama `ly` `dirty` with poster translations in `zh-rCN` (`.jpg`) and `en` (`.png`)
- **WHEN** the worker handles `SyncDramaJob('ly')`
- **THEN** OSS server-side copies `Drama/staging/ly/poster/zh-rCN.jpg` → `Drama/prod/ly/poster/zh-rCN.jpg`
- **AND** OSS server-side copies `Drama/staging/ly/poster/en.png` → `Drama/prod/ly/poster/en.png`
- **AND** the `POST /sync/dramas` payload's `translations["zh-rCN"].poster_url` is `"https://photobundle.../Drama/prod/ly/poster/zh-rCN.jpg"`
- **AND** `translations["en"].poster_url` is `"https://photobundle.../Drama/prod/ly/poster/en.png"`

#### Scenario: drama sync omits poster_url for language with no poster
- **GIVEN** drama `ly` has `name` translation in `ja` but no poster file for `ja`
- **WHEN** the worker handles `SyncDramaJob('ly')`
- **THEN** the payload's `translations["ja"].poster_url` is `null`
- **AND** no `Drama/prod/ly/poster/ja.*` object is created

#### Scenario: episode sync copies cover and subtitles to prod
- **GIVEN** episode `ly-ep-3` `dirty` with cover and subtitles in `en` and `zh-rCN`
- **WHEN** the worker handles `SyncEpisodeJob('ly', 3)`
- **THEN** OSS server-side copies cover staging→prod
- **AND** OSS server-side copies each subtitle staging→prod (two copies)
- **AND** the `POST /sync/episodes` payload's `cover_url` is `"https://photobundle.../Drama/prod/ly/ep-3/cover.jpg"`
- **AND** `subtitles[*].url` are absolute prod OSS URLs

#### Scenario: drama sync success enqueues children
- **GIVEN** drama `ly` `dirty` with episodes 1 (`dirty`), 2 (`clean`), 3 (`pending_delete`)
- **WHEN** the worker handles `SyncDramaJob('ly')` and the business server returns 200
- **THEN** the drama row is `clean` with `last_synced_at` set
- **AND** two episode jobs are enqueued: `SyncEpisodeJob('ly', 1)` and `SyncEpisodeJob('ly', 3)`
- **AND** episode 2's row is unchanged (still `clean`)

#### Scenario: drama sync failure stops mid-flow
- **GIVEN** drama `ly` `dirty` with one dirty episode
- **WHEN** the worker handles `SyncDramaJob('ly')` and the business server returns 500
- **THEN** the drama row is `sync_failed` with `sync_error` containing the upstream error
- **AND** no episode job is enqueued
- **AND** the dirty episode's `sync_status` is unchanged

#### Scenario: episode pending_delete sync sweeps prod by prefix
- **GIVEN** episode `ly-ep-3` `pending_delete`; drama `ly` previously synced
- **WHEN** the worker handles `SyncEpisodeJob('ly', 3)` and `DELETE /sync/episodes/ly/3` returns 204
- **THEN** `unpublish_episode_from_prod('ly', 'ep-3')` is called once
- **AND** all `Drama/prod/ly/ep-3/...` objects are gone (covers ladders, cover, subtitles)
- **AND** the episodes row for `(ly, 3)` is physically gone from the DB

#### Scenario: episode sync fails when staging asset is missing
- **GIVEN** episode `ly-ep-3` `dirty`, but `Drama/staging/ly/ep-3/cover.jpg` was manually deleted from OSS
- **WHEN** the worker handles `SyncEpisodeJob('ly', 3)`
- **THEN** `publish_cover_to_prod` raises `PublishError`
- **AND** the row is set to `sync_failed` with the error mentioning the missing staging cover
- **AND** the business server is NOT called

### Requirement: business server `/sync/*` wire protocol

The business server (separate codebase) SHALL expose these four endpoints. Each request MUST carry `X-API-Key: <shared secret>`; mismatch → 401. Each request body is JSON `application/json`.

**`POST /sync/dramas`** — request body:
```
{
  "slug": str,                                 // matches ^[a-z0-9][a-z0-9-]*$
  "default_lang": str,                         // matches a `code` in this payload's `languages`
  "client_updated_at": str,                    // ISO 8601
  "translations": {                            // by lang_code
    "<lang_code>": {
      "name": str,                             // required (the drama-meta-translations invariant)
      "synopsis": str | null,
      "poster_url": str | null                 // absolute prod OSS URL, opaque (do NOT fetch bytes)
    }
  },
  "tags":   [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "actors": [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "languages": [ {"code": str, "display_label": str} ]
}
```

**Field semantics changed**: `translations[lang].poster_url` is now an **absolute prod OSS URL** (e.g. `https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/ly/poster/zh-rCN.jpg`) — not a relative path. The bytes already exist at that URL by the time the business server receives the request (HLS-side did the staging→prod copy first). The business server MUST treat the URL as opaque: store it verbatim, return it verbatim to SDK clients.

The business server MUST NOT fetch the URL to validate it or to mirror the bytes locally.

The business server MUST: validate the API key; upsert language rows; upsert tag rows + tag translations; upsert actor rows + actor translations; upsert drama row + drama translations (storing the opaque URLs as-is). On success → 200 `{"ok": true, "client_updated_at": "...", "synced_at": "..."}`. If the supplied `client_updated_at` is older than what is already stored → 409.

**`DELETE /sync/dramas/{slug}`** — no body. Removes the drama and every cascading row (episodes, translations, tags-for-this-drama-only relations). Returns 204 on success or if the drama did not exist (idempotent). 401 on key mismatch.

**`POST /sync/episodes`** — request body:
```
{
  "drama_slug": str,
  "ep_number": int,
  "episode_id": str,                           // "{drama_slug}-ep-{ep_number}"
  "client_updated_at": str,
  "duration_ms": int,
  "width": int | null,
  "height": int | null,
  "drm": {
    "key_uri": str,                            // verbatim "/drm/{slug}/ep-{n}/key"
    "key_base64": str,                         // base64 of 16-byte AES key
    "iv_hex": str | null                       // 32 hex chars
  },
  "playlists": {
    "540p": str,                               // full m3u8 text, references prod OSS URLs
    "720p": str,
    "1080p": str
  },
  "cover_url": str,                            // absolute prod OSS URL, opaque
  "subtitles": [
    {
      "lang_code": str,
      "label": str,                            // languages.display_label snapshot
      "url": str                               // absolute prod OSS URL, opaque
    }
  ]
}
```

**Field semantics changed**: `cover_url` and each `subtitles[].url` are now **absolute prod OSS URLs**. The business server MUST treat them as opaque (record verbatim) and MUST NOT fetch the bytes.

The business server MUST: validate the API key; ensure the drama exists (else 409 "drama not synced"); decode `drm.key_base64` and write 16 bytes to its own keys directory; write each playlist text to its own `.m3u8` file; upsert the episode row (storing the opaque URLs as-is). On success → 200. Old `client_updated_at` → 409.

**The 502 status code is REMOVED from the protocol.** Previously 502 indicated the business server failed to fetch a poster / cover / subtitle URL. Since the business server no longer fetches, this failure mode does not exist.

**`DELETE /sync/episodes/{slug}/{ep}`** — no body. Removes the episode row + on-disk m3u8 / DRM key files on the business server. Returns 204 on success or if missing (idempotent). 401 on key mismatch.

#### Scenario: drama sync request carries absolute prod URLs
- **GIVEN** drama `ly` (default_lang=`zh-rCN`) with poster translations in `zh-rCN` (.jpg) and `en` (.png)
- **WHEN** the HLS sync worker calls `POST /sync/dramas`
- **THEN** `payload.translations["zh-rCN"].poster_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/ly/poster/zh-rCN.jpg"`
- **AND** `payload.translations["en"].poster_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/ly/poster/en.png"`
- **AND** the request carries `X-API-Key`

#### Scenario: episode sync request carries absolute prod URLs for cover and subtitles
- **GIVEN** episode `ly-ep-3` ready, subtitles in `en`
- **WHEN** the HLS sync worker calls `POST /sync/episodes`
- **THEN** `payload.cover_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/ly/ep-3/cover.jpg"`
- **AND** `payload.subtitles[0].url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/ly/ep-3/subtitles/en.vtt"`
- **AND** `payload.playlists.720p` contains `#EXT-X-MAP:URI="https://photobundle.../Drama/prod/ly/ep-3/720p/init-720p.mp4"`
- **AND** `payload.playlists.720p` contains `#EXT-X-KEY:METHOD=AES-128,URI="/drm/ly/ep-3/key"...` (verbatim)

#### Scenario: business server stores URLs opaque and does not fetch
- **GIVEN** the business server has just received `POST /sync/episodes` with absolute OSS URLs
- **WHEN** it processes the request
- **THEN** no outbound HTTP GET is made to OSS or to the HLS server for cover/subtitle bytes
- **AND** the stored `EpisodeInfo.cover_url` is the verbatim payload value
- **AND** clients receive the same absolute OSS URL when they query the SDK API

#### Scenario: API key mismatch returns 401
- **GIVEN** the business server is running with a different `X-API-Key` than the HLS server is sending
- **WHEN** the HLS worker calls any `/sync/*` endpoint
- **THEN** the response is 401
- **AND** the corresponding HLS row is set to `sync_failed` with `sync_error` mentioning 401
