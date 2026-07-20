## MODIFIED Requirements

### Requirement: business server `/sync/*` wire protocol

The business server (separate codebase to be built later) SHALL expose these five endpoints. Each request MUST carry `X-API-Key: <shared secret>`; mismatch → 401. Each request body is JSON `application/json`.

**`POST /sync/dramas`** — request body:
```
{
  "slug": str,                                 // matches ^[a-z0-9][a-z0-9-]*$
  "default_lang": str,                         // matches a `code` in this payload's `languages`
  "free_episodes": int,                        // 0 = all paid; N = first N episodes free
  "is_ongoing": bool,                          // true = serializing; false = completed
  "client_updated_at": str,                    // ISO 8601
  "translations": {                            // by lang_code
    "<lang_code>": {
      "name": str,                             // required (the drama-meta-translations invariant)
      "synopsis": str | null,
      "poster_key": str | null,                // prod object key for portrait poster
      "poster_landscape_key": str | null       // prod object key for landscape poster
    }
  },
  "tags":   [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "actors": [ {"slug": str, "default_lang": str, "translations": {"<lang_code>": str}} ],
  "languages": [ {"code": str, "display_label": str} ]
}
```

The business server MUST: validate the API key; upsert language rows; upsert tag rows + tag translations; upsert actor rows + actor translations; upsert drama row including `free_episodes` and `is_ongoing` + drama translations; persist `poster_key` / `poster_landscape_key` as opaque prod object keys. On success → 200 `{"ok": true, "client_updated_at": "...", "synced_at": "..."}`. If the supplied `client_updated_at` is older than what is already stored → 409 (defensive against out-of-order overwrites).

**`DELETE /sync/dramas/{slug}`** — no body. Removes the drama and every cascading row (episodes, translations, tags-for-this-drama-only relations, posters on disk). Returns 204 on success or if the drama did not exist (idempotent). 401 on key mismatch.

**`POST /sync/episodes`** — request body:
```
{
  "drama_slug": str,
  "ep_number": int,
  "episode_id": str,                           // "{drama_slug}-ep-{ep_number}"
  "client_updated_at": str,
  "duration_ms": int,
  "drm": {
    "key_uri": str,                            // verbatim "/drm/{slug}/ep-{n}/key"
    "key_base64": str,                         // 24-char base64 of 16-byte AES key
    "iv_hex": str | null                       // 32 hex chars
  },
  "video_tracks": [                            // mirrors EpisodeInfo.videoTracks, ordered high → mid → low
    {
      "id": str,                               // "high" | "mid" | "low"
      "ladder": str,                           // "1080p" | "720p" | "540p"
      "width": int | null,                     // this rung's encoded width (null on legacy rows)
      "height": int | null,                    // this rung's encoded height
      "playlist": str                          // full prod m3u8 text for this rung
    }
  ],
  "cover_key": str,                            // prod object key (storage on) or staging /videos URL (storage off)
  "subtitles": [
    {
      "lang_code": str,
      "label": str,                            // languages.display_label snapshot
      "key": str                               // prod object key (storage on) or /videos URL (storage off)
    }
  ]
}
```

The business server MUST: validate the API key; ensure the drama exists (else 409 "drama not synced"); resolve `cover_key` and every subtitle `key` (prefix with `MEDIA_BASE_URL` when storage is on, else pull from the staging URL) — any failure → 502; decode `drm.key_base64` and write 16 bytes to its own keys directory; write each `video_tracks[].playlist` text to its own `.m3u8` file; assemble the SDK's `videoTracks` directly from each entry's `id` / `width` / `height` (no per-rung dimension re-derivation); persist cover and subtitle bytes locally; upsert the episode row. On success → 200. Old `client_updated_at` → 409.

> **Wire-protocol change** (per-rung `video_tracks` replaces the old single top-level `width` / `height` + `playlists` map): the old top-level dimensions described only the *source*, not any rung, so the business server could not build accurate per-rung `videoTracks` from them. The HLS side now ships one entry per rung. The business server's `POST /sync/episodes` parser MUST be updated in lockstep — it can no longer read `payload.playlists.{ladder}` or `payload.width` / `payload.height`.

**`DELETE /sync/episodes/{slug}/{ep}`** — no body. Removes the episode row + on-disk artifacts on the business server. Returns 204 on success or if missing (idempotent). 401 on key mismatch.

**`PUT /sync/featured-categories`** — request body:
```
{
  "categories": {
    "recommend": [str],                        // ordered drama slugs
    "new": [str],
    "hot": [str],
    "exclusive": [str]
  }
}
```

The payload MUST contain exactly the four fixed category keys. Each array SHALL be treated as an ordered list and MUST NOT contain duplicate slugs. The business server MUST validate that every referenced drama already exists and atomically replace all four category collections; an empty array clears that category. On success it SHALL return 200 `{"ok": true}`. A missing drama SHALL return 409 without changing any category.

#### Scenario: drama sync request shape excludes featured categories
- **GIVEN** drama `ly` (default_lang=`zh-rCN`) with translations in `zh-rCN` and `en`, tags `[urban]`, actors `[zhang-san]`
- **WHEN** the HLS sync worker calls `POST /sync/dramas`
- **THEN** the request body matches the schema above and does not contain `featured_categories`
- **AND** carries header `X-API-Key: <configured secret>`
- **AND** `payload.languages` includes `zh-rCN` and `en`

#### Scenario: episode sync request shape includes per-rung prod m3u8
- **GIVEN** episode `ly-ep-3` ready (source 720×1280), with subtitles in `en`
- **WHEN** the HLS sync worker calls `POST /sync/episodes`
- **THEN** `payload.video_tracks` has three entries ordered `high` (1080p), `mid` (720p), `low` (540p)
- **AND** each entry carries the rung's encoded `width` / `height` (e.g. `mid` → 406×720), derived identically to `EpisodeInfo.videoTracks`
- **AND** the `mid` entry's `playlist` is a full m3u8 text whose `#EXT-X-MAP:URI` references `Drama/prod/ly/ep-3/720p/init-720p.mp4`
- **AND** the `mid` entry's `playlist` contains `#EXT-X-KEY:METHOD=AES-128,URI="/drm/ly/ep-3/key"...` (verbatim)
- **AND** `payload.cover_key` is the prod cover object key (storage on) or staging URL (storage off)

#### Scenario: re-uploaded episode sync uses versioned ancillary assets
- **GIVEN** episode `ly-ep-3` has `upload_version=2`
- **WHEN** the HLS sync worker calls `POST /sync/episodes`
- **THEN** playlist object keys, `payload.cover_key`, and subtitle keys use the `Drama/prod/ly/ep-3-v2/...` prefix
- **AND** the sync worker does not copy legacy staging objects for compatibility; missing prod objects are caught by offline audit

#### Scenario: featured category snapshot replaces all categories
- **GIVEN** the business server has all referenced dramas
- **WHEN** HLS calls `PUT /sync/featured-categories` with ordered arrays for all four categories
- **THEN** the business server atomically persists those exact arrays and returns 200

#### Scenario: featured category snapshot rejects a missing drama atomically
- **GIVEN** one payload array references an unknown drama slug
- **WHEN** HLS calls `PUT /sync/featured-categories`
- **THEN** the business server returns 409
- **AND** none of the four stored categories change

#### Scenario: API key mismatch returns 401
- **GIVEN** the business server is running with a different `X-API-Key` than the HLS server is sending
- **WHEN** HLS calls any `/sync/*` endpoint
- **THEN** the response is 401
- **AND** drama / episode workers mark their corresponding HLS row `sync_failed`; the direct featured-category action returns 502 to its operator

## ADDED Requirements

### Requirement: HLS featured category sync action

The HLS service SHALL provide `POST /admin/featured-categories/sync`, protected by the existing `can_sync` permission. If business sync is disabled it SHALL return 503. Otherwise it SHALL synchronously send the current complete ordered category snapshot to `PUT /sync/featured-categories`. Business-server non-2xx responses and network errors SHALL return 502 to the operator without changing local category data.

#### Scenario: operator publishes current category snapshot
- **GIVEN** business sync is configured and local categories contain ordered drama lists
- **WHEN** an authorized operator posts `/admin/featured-categories/sync`
- **THEN** HLS sends one `PUT /sync/featured-categories` request using the configured API key
- **AND** returns 200 only after the business server accepts it

#### Scenario: sync permission is required
- **GIVEN** a staff account without `can_sync`
- **WHEN** it posts `/admin/featured-categories/sync`
- **THEN** the response is 403

#### Scenario: disabled sync returns 503
- **GIVEN** `BUSINESS_SYNC_BASE_URL` is unset
- **WHEN** an authorized operator posts `/admin/featured-categories/sync`
- **THEN** the response is 503 and no outbound request occurs
