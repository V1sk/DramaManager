## ADDED Requirements

### Requirement: sync state machine schema

The `dramas` and `episodes` tables SHALL each carry three additional columns:
- `sync_status` (TEXT NOT NULL DEFAULT `'dirty'`, value ∈ {`dirty`, `syncing`, `clean`, `sync_failed`, `pending_delete`}).
- `sync_error` (TEXT, nullable; populated only when `sync_status='sync_failed'`).
- `last_synced_at` (TEXT, nullable, ISO 8601 UTC; populated on first successful sync and refreshed on every successful sync).

State transitions SHALL be enforced at the application layer:
- New drama / new episode → `dirty`.
- Mutating endpoint that affects synced state → `dirty`.
- Sync action → `syncing` → `clean` (success) or `sync_failed` (failure).
- Delete on already-synced row → `pending_delete`.
- Delete-sync success → row physically removed.
- On process restart, every row with `sync_status='syncing'` SHALL be flipped to `sync_failed` with `sync_error='orphaned by restart'`, mirroring the pipeline worker's reap-on-startup behavior.

#### Scenario: schema includes sync columns
- **WHEN** the service starts with an empty DB and `init_db()` runs
- **THEN** `PRAGMA table_info(dramas)` and `PRAGMA table_info(episodes)` each include `sync_status`, `sync_error`, `last_synced_at`

#### Scenario: orphaned syncing rows are reaped on restart
- **GIVEN** a drama row with `sync_status='syncing'` from a prior crashed process
- **WHEN** the service starts
- **THEN** that row's `sync_status` is `'sync_failed'`
- **AND** `sync_error` is `'orphaned by restart'`

### Requirement: dirty-marking on mutations

Endpoints that change state visible to prod SHALL set the affected drama or episode to `sync_status='dirty'` and refresh its `updated_at`. Cascade rules apply to library mutations:

**Drama-direct triggers (mark drama dirty):**
- `POST /admin/dramas` (creation)
- `PATCH /admin/dramas/{slug}` (default_lang change)
- `PUT /admin/dramas/{slug}/translations/{lang_code}` (name / synopsis upsert)
- `DELETE /admin/dramas/{slug}/translations/{lang_code}` (translation removal)
- `POST /admin/dramas/{slug}/poster` (poster upload)
- `DELETE /admin/dramas/{slug}/poster` (poster removal)
- `PUT /admin/dramas/{slug}/tags`
- `PUT /admin/dramas/{slug}/actors`

**Episode-direct triggers (mark episode dirty):**
- `POST /admin/dramas/{slug}/episodes` (initial upload)
- `POST /admin/dramas/{slug}/episodes/{ep}` (re-upload)
- `POST /api/episodes/{slug}/{ep}/cover` (cover replace)
- `POST /admin/episodes/{slug}/{ep}/subtitles?lang=...` (subtitle upload/replace)
- `DELETE /admin/episodes/{slug}/{ep}/subtitles?lang=...` (subtitle delete)

**Library cascade (mark every dramat referencing the entity dirty):**
- `PATCH /admin/tags/{slug}` / `PUT /admin/tags/{slug}/translations/{lang_code}` / `DELETE /admin/tags/{slug}/translations/{lang_code}` → every drama with this tag in `drama_tags` goes dirty (unless already `pending_delete`).
- `PATCH /admin/actors/{slug}` / actor translation upsert/delete → every drama with this actor in `drama_actors` goes dirty.
- `PATCH /admin/languages/{code}` → every drama whose episodes have a subtitle in this language goes dirty (label travels with subtitle metadata).

A drama or episode in `pending_delete` SHALL NOT be re-marked dirty by any mutation; it remains `pending_delete` until the next sync removes it.

#### Scenario: editing drama synopsis dirties only the drama
- **GIVEN** drama `ly` with `sync_status='clean'` and 3 episodes, all `clean`
- **WHEN** the operator PUTs a new synopsis to `/admin/dramas/ly/translations/zh-rCN`
- **THEN** `dramas.ly.sync_status='dirty'`
- **AND** every episode of `ly` retains `sync_status='clean'`

#### Scenario: re-uploading an episode dirties only the episode
- **GIVEN** drama `ly` clean, episode `ly-ep-3` clean
- **WHEN** the operator POSTs a new video to `/admin/dramas/ly/episodes/3`
- **THEN** `episodes.ly-ep-3.sync_status='dirty'`
- **AND** `dramas.ly.sync_status` is unchanged (still `clean`)

#### Scenario: tag rename cascades to using dramas
- **GIVEN** dramas `a`, `b`, `c` all `clean`; tag `urban` is referenced by `a` and `b` only
- **WHEN** the operator PUTs a new English label to `/admin/tags/urban/translations/en`
- **THEN** `dramas.a.sync_status='dirty'` AND `dramas.b.sync_status='dirty'`
- **AND** `dramas.c.sync_status='clean'` (not affected)

#### Scenario: cascade respects pending_delete
- **GIVEN** drama `gone` with `sync_status='pending_delete'`; tag `urban` references it via `drama_tags`
- **WHEN** the operator updates the tag's translation
- **THEN** `dramas.gone.sync_status` remains `'pending_delete'`

### Requirement: HLS-side sync action endpoints

The service SHALL provide `POST /admin/dramas/{slug}/sync`. The endpoint validates `slug` regex; 404 if drama missing; 503 if `BUSINESS_SYNC_BASE_URL` is not configured. Otherwise it enqueues a `SyncDramaJob(slug)` onto the sync queue, transitions the drama's `sync_status` to `'syncing'` (or leaves it `pending_delete` for a delete sync), and responds 202 with the current drama row.

The service SHALL provide `POST /admin/episodes/{slug}/{ep}/sync`. Validates `slug` and `ep`; 404 if episode missing; 503 if sync disabled; **409** if `dramas.{slug}.last_synced_at IS NULL` (drama has never been synced). Otherwise enqueues a `SyncEpisodeJob(slug, ep)`, transitions the episode's `sync_status` to `'syncing'` (preserving `pending_delete` for delete syncs), responds 202.

The endpoints SHALL be no-ops when the target is already `clean`: they SHALL return 200 with the row unchanged and SHALL NOT enqueue.

#### Scenario: sync drama enqueues and returns 202
- **GIVEN** drama `ly` `dirty`, sync configured
- **WHEN** the operator calls `POST /admin/dramas/ly/sync`
- **THEN** the response is 202
- **AND** `dramas.ly.sync_status='syncing'`
- **AND** the sync queue contains a `SyncDramaJob('ly')`

#### Scenario: sync episode rejected when drama never synced
- **GIVEN** drama `ly` with `last_synced_at IS NULL`; episode `ly-ep-3` `dirty`
- **WHEN** the operator calls `POST /admin/episodes/ly/3/sync`
- **THEN** the response is 409 with a message naming the precondition
- **AND** the episode row is unchanged (still `dirty`)

#### Scenario: sync on a clean target is a no-op
- **GIVEN** drama `ly` `clean`
- **WHEN** the operator calls `POST /admin/dramas/ly/sync`
- **THEN** the response is 200
- **AND** the row's `sync_status` remains `clean`
- **AND** no job is enqueued

#### Scenario: sync disabled returns 503
- **GIVEN** the service was started without `BUSINESS_SYNC_BASE_URL`
- **WHEN** the operator calls any `/admin/.../sync` endpoint
- **THEN** the response is 503 with a body explaining sync is not configured

### Requirement: sync worker behavior

The service SHALL run a single background coroutine `sync_worker_loop` consuming a module-level `asyncio.Queue` (`sync_queue`). Job types: `SyncDramaJob(slug)` and `SyncEpisodeJob(slug, ep_number)`.

For `SyncDramaJob`:
1. Read drama row.
2. If `sync_status='pending_delete'`: call `DELETE /sync/dramas/{slug}` against the business server; on 2xx, call `unpublish_drama_from_prod(slug)`, then physically delete the drama row from the local DB. On non-2xx, set `sync_status='sync_failed'` with the error.
3. Otherwise (status=`syncing`): build the `POST /sync/dramas` payload (drama row + translations + tags inline + actors inline + languages-used-by-drama inline). Call the business server. On 2xx, set `sync_status='clean'`, refresh `last_synced_at`. On non-2xx, set `sync_status='sync_failed'` with the error and stop (do not enqueue child episodes).
4. After a successful drama upsert, enqueue a `SyncEpisodeJob` for every episode of this drama whose `sync_status ∈ {dirty, pending_delete}`.

For `SyncEpisodeJob`:
1. Read episode row + drama row.
2. If `dramas.{slug}.last_synced_at IS NULL`: set `sync_status='sync_failed'` with `sync_error='drama not synced'`. Stop.
3. If episode's `sync_status='pending_delete'`: call `DELETE /sync/episodes/{slug}/{ep}`; on 2xx, call `unpublish_ladder_from_prod` for each of (540p, 720p, 1080p), then physically delete the episode row.
4. Otherwise: call `publish_ladder_to_prod` for each ladder (collect prod-flavored m3u8 strings). Build the `POST /sync/episodes` payload with those strings + cover/subtitle URLs. Call the business server. On 2xx, set `sync_status='clean'`, refresh `last_synced_at`. On non-2xx, set `sync_status='sync_failed'`.

The worker SHALL handle exceptions per-job — a failure in one job MUST NOT crash the worker. The worker MUST mark the job's row `sync_failed` with a useful error before continuing.

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

#### Scenario: episode pending_delete sync removes prod and physical row
- **GIVEN** episode `ly-ep-3` `pending_delete`; drama `ly` previously synced
- **WHEN** the worker handles `SyncEpisodeJob('ly', 3)` and `DELETE /sync/episodes/ly/3` returns 204
- **THEN** `unpublish_ladder_from_prod` is called for each of 540p, 720p, 1080p
- **AND** the episodes row for `(ly, 3)` is physically gone from the DB

### Requirement: business server `/sync/*` wire protocol

The business server (separate codebase to be built later) SHALL expose these four endpoints. Each request MUST carry `X-API-Key: <shared secret>`; mismatch → 401. Each request body is JSON `application/json`.

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
      "poster_url": str | null                 // absolute https URL the business server pulls
    }
  },
  "tags":   [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "actors": [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "languages": [ {"code": str, "display_label": str} ]
}
```

The business server MUST: validate the API key; pull every non-null `poster_url` (any pull failure → 502 with the failing URL named); upsert language rows; upsert tag rows + tag translations; upsert actor rows + actor translations; upsert drama row + drama translations; persist poster bytes locally. On success → 200 `{"ok": true, "client_updated_at": "...", "synced_at": "..."}`. If the supplied `client_updated_at` is older than what is already stored → 409 (defensive against out-of-order overwrites).

**`DELETE /sync/dramas/{slug}`** — no body. Removes the drama and every cascading row (episodes, translations, tags-for-this-drama-only relations, posters on disk). Returns 204 on success or if the drama did not exist (idempotent). 401 on key mismatch.

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
    "key_base64": str,                         // 24-char base64 of 16-byte AES key
    "iv_hex": str | null                       // 32 hex chars
  },
  "playlists": {
    "540p": str,                               // full m3u8 text with prod URLs
    "720p": str,
    "1080p": str
  },
  "cover_url": str,                            // staging URL
  "subtitles": [
    {
      "lang_code": str,
      "label": str,                            // languages.display_label snapshot
      "url": str                               // staging URL
    }
  ]
}
```

The business server MUST: validate the API key; ensure the drama exists (else 409 "drama not synced"); pull cover and every subtitle URL synchronously (any failure → 502); decode `drm.key_base64` and write 16 bytes to its own keys directory; write each playlist text to its own `.m3u8` file; persist cover and subtitle bytes locally; upsert the episode row. On success → 200. Old `client_updated_at` → 409.

**`DELETE /sync/episodes/{slug}/{ep}`** — no body. Removes the episode row + on-disk artifacts on the business server. Returns 204 on success or if missing (idempotent). 401 on key mismatch.

#### Scenario: drama sync request shape
- **GIVEN** drama `ly` (default_lang=`zh-rCN`) with translations in `zh-rCN` and `en`, tags `[urban]`, actors `[zhang-san]`
- **WHEN** the HLS sync worker calls `POST /sync/dramas`
- **THEN** the request body matches the schema above
- **AND** carries header `X-API-Key: <configured secret>`
- **AND** `payload.languages` includes `zh-rCN` and `en` (every code referenced by translations / tags / actors)

#### Scenario: episode sync request shape includes prod m3u8
- **GIVEN** episode `ly-ep-3` ready, with subtitles in `en`
- **WHEN** the HLS sync worker calls `POST /sync/episodes`
- **THEN** `payload.playlists.720p` is a full m3u8 text whose `#EXT-X-MAP:URI` references `Drama/prod/ly/ep-3/720p/init-720p.mp4`
- **AND** `payload.playlists.720p` contains `#EXT-X-KEY:METHOD=AES-128,URI="/drm/ly/ep-3/key"...` (verbatim)
- **AND** `payload.cover_url` is an absolute https URL pointing at the staging server's `/videos/ly/ep-3/cover.jpg`

#### Scenario: API key mismatch returns 401
- **GIVEN** the business server is running with a different `X-API-Key` than the HLS server is sending
- **WHEN** the HLS worker calls any `/sync/*` endpoint
- **THEN** the response is 401
- **AND** the corresponding HLS row is set to `sync_failed` with `sync_error` mentioning 401

### Requirement: HLS-side configuration

The service SHALL read these env vars at startup:
- `BUSINESS_SYNC_BASE_URL`: optional. When unset, sync features are disabled (UI placeholders stay disabled, action endpoints return 503). When set, MUST start with `https://` (or `http://` for internal-network deployments) and MUST NOT end with `/`.
- `BUSINESS_SYNC_API_KEY`: required iff `BUSINESS_SYNC_BASE_URL` is set; missing → fail-fast at startup with a clear error. The value is sent as `X-API-Key` on every `/sync/*` request.
- `BUSINESS_SYNC_TIMEOUT`: optional, integer seconds, default 30. Per-request HTTP timeout for the sync worker's HTTP client.

The startup log SHALL state whether sync is enabled and (when enabled) the configured base URL (without leaking the API key).

#### Scenario: sync enabled at startup
- **GIVEN** `BUSINESS_SYNC_BASE_URL=https://prod.internal` and `BUSINESS_SYNC_API_KEY=secret`
- **WHEN** the service starts
- **THEN** the startup log states `sync enabled; base=https://prod.internal`
- **AND** sync UI elements render (badges, buttons)
- **AND** action endpoints behave per the rest of this spec

#### Scenario: sync disabled when base URL unset
- **GIVEN** `BUSINESS_SYNC_BASE_URL` is not set
- **WHEN** the service starts
- **THEN** the startup log states `sync disabled`
- **AND** `POST /admin/dramas/{slug}/sync` returns 503

#### Scenario: missing API key with set base URL fails fast
- **GIVEN** `BUSINESS_SYNC_BASE_URL=https://prod.internal` but `BUSINESS_SYNC_API_KEY` not set
- **WHEN** the service starts
- **THEN** the process exits non-zero with a clear error naming the missing var

### Requirement: sync overview page

The service SHALL serve `GET /admin/sync` returning an HTML page (extending the shared admin base layout) that lists every drama with `sync_status ∈ {dirty, syncing, sync_failed, pending_delete}` and every episode with the same statuses. Each row displays the slug/episode_id, current `sync_status`, last error (if any), `last_synced_at`, and action buttons:
- For `dirty` / `sync_failed`: "[同步]" button (POSTs to the corresponding sync endpoint).
- For `syncing`: a spinner; the page polls every 2 seconds.
- For `pending_delete`: "[同步删除]" button (same POST endpoint; the worker handles the delete sync transparently).
- For `sync_failed`: a "[查看错误]" disclosure showing `sync_error`.

The page SHALL also offer a "[同步全部]" button that enqueues a sync for every dirty / sync_failed / pending_delete drama (the worker fans out to episodes as needed).

The nav-bar `<div id="sync-zone">` SHALL render `需同步: N` (linking to `/admin/sync`) where N is the total count of non-clean drama and episode rows. When N=0 the link reads `已同步`.

#### Scenario: overview page lists non-clean rows
- **GIVEN** dramas `a` (clean), `b` (dirty); episodes `b-ep-1` (dirty), `b-ep-2` (clean)
- **WHEN** the operator opens `/admin/sync`
- **THEN** the page shows rows for drama `b` and episode `b-ep-1`
- **AND** does NOT show drama `a` or episode `b-ep-2`

#### Scenario: bulk sync enqueues every non-clean drama
- **GIVEN** dramas `a` (clean), `b` (dirty), `c` (sync_failed)
- **WHEN** the operator clicks "[同步全部]"
- **THEN** sync jobs are enqueued for `b` and `c`
- **AND** their `sync_status` becomes `syncing` (or stays `pending_delete` if applicable)
- **AND** drama `a` is unaffected
