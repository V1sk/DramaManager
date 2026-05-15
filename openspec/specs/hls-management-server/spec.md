### Requirement: Synchronous metadata extraction

Before persisting the episode row the service SHALL, in order: (1) probe the uploaded video with `ffprobe` to obtain `duration_ms` as an integer number of milliseconds; (2) extract the first frame of the uploaded video with `ffmpeg` (`-ss 0 -vframes 1 -vf scale=-2:720`) to `OUT_DIR/{drama_slug}/ep-{n}/cover.jpg`. Both steps run before the HTTP response returns.

#### Scenario: Duration and cover are produced before response
- **WHEN** a valid upload completes
- **THEN** `OUT_DIR/{drama_slug}/ep-{n}/cover.jpg` exists on disk
- **AND** the DB row carries a non-null `duration_ms` and a non-null `cover_url`
- **BEFORE** the 302 response returns to the client

#### Scenario: ffprobe failure aborts the upload
- **WHEN** `ffprobe` exits non-zero against the uploaded file
- **THEN** the service responds 400 and stores nothing in the DB
- **AND** the temporary upload file is deleted

### Requirement: Persistence schema

The service SHALL persist each episode in a SQLite table named `episodes` with the columns `id`, `drama_slug`, `ep_number`, `episode_id`, `status`, `duration_ms`, `play_url`, `key_uri`, `key_b64`, `iv_hex`, `cover_url`, `width`, `height`, `source_filename`, `error_message`, `created_at`, `updated_at`. The column `drama_name` SHALL NOT exist on this table — drama name lives on the `dramas` row. The tuple `(drama_slug, ep_number)` SHALL be unique and `episode_id` SHALL be `"{drama_slug}-ep-{ep_number}"` and itself unique. A foreign key constraint SHALL link `episodes.drama_slug` to `dramas.slug` with `ON DELETE RESTRICT`.

#### Scenario: Re-uploading the same slug and episode overwrites
- **GIVEN** `dramas` has `slug='langyabang'` and `episodes` has a row with `drama_slug=langyabang`, `ep_number=3`, `status=ready`
- **WHEN** a new valid upload arrives with the same `drama_slug` and `ep_number`
- **THEN** the existing episode row is updated in place (`status` reset to `pending`, `updated_at` refreshed, `error_message` cleared, DRM fields cleared)
- **AND** no duplicate row is created
- **AND** the `dramas` row is not touched

#### Scenario: Status lifecycle is enforced
- **WHEN** an upload is first persisted
- **THEN** `status=pending`
- **WHEN** the background worker picks up the job
- **THEN** `status=encoding`
- **WHEN** pipeline.sh exits 0 and DRM fields are filled in
- **THEN** `status=ready`
- **WHEN** pipeline.sh exits non-zero
- **THEN** `status=failed` and `error_message` is populated

#### Scenario: drama_name column is absent from the schema
- **WHEN** the service initializes the DB (`init_db()`)
- **THEN** `PRAGMA table_info(episodes)` does not list a `drama_name` column

### Requirement: Serialized pipeline execution

The service SHALL enqueue each accepted upload into a single global `asyncio.Queue` consumed by exactly one worker coroutine. At most one `pipeline.sh` invocation SHALL run at a time across the whole process. Job order SHALL be FIFO.

#### Scenario: Concurrent uploads are serialized
- **GIVEN** upload A is in progress and pipeline.sh is running
- **WHEN** upload B arrives
- **THEN** upload B is persisted (synchronous cover + duration extracted) and enqueued
- **AND** pipeline.sh for B does not start until pipeline.sh for A has exited

### Requirement: Pipeline invocation

The worker SHALL invoke `pipeline.sh` with exactly these arguments, quoted safely:
- `<source>` = absolute path of the temporary upload file
- `<output_dir>` = absolute path of `OUT_DIR/{drama_slug}`
- `<episode_id>` = `ep-{ep_number}`
- `<key_uri_base>` = `{PUBLIC_BASE_URL}/drm/{drama_slug}/ep-{ep_number}/key`

After a successful exit the worker SHALL read `OUT_DIR/{drama_slug}/keys/ep-{n}.key.b64` and `OUT_DIR/{drama_slug}/keys/ep-{n}.iv` and persist `key_b64`, `iv_hex`, `key_uri` (matching `<key_uri_base>`), and `play_url` (`{PUBLIC_BASE_URL}/videos/{drama_slug}/ep-{n}/720p/media-720p.m3u8`). After either success or failure the worker SHALL delete the temporary upload file.

#### Scenario: Successful pipeline transitions to ready
- **WHEN** pipeline.sh exits 0 for `(langyabang, 3)`
- **THEN** the row has `status=ready`, `play_url` populated, `key_uri` equal to the URL passed as argument 4, `key_b64` equal to the contents of `ep-3.key.b64`, `iv_hex` equal to the contents of `ep-3.iv`
- **AND** the temporary upload file has been removed

#### Scenario: Failed pipeline surfaces stderr
- **WHEN** pipeline.sh exits non-zero
- **THEN** the row has `status=failed` and `error_message` contains the last 4 KiB of combined stderr
- **AND** pipeline artifacts under `OUT_DIR/{drama_slug}/ep-{n}/` are NOT deleted by the service
- **AND** the temporary upload file has been removed

### Requirement: SDK episode-info endpoint

The service SHALL serve `GET /api/episodes/{drama_slug}/{ep}` where `{ep}` matches `^[0-9]+$`. When a row exists with `status=ready` the response SHALL be JSON that strictly validates against `episode-info-schema.json` with these field mappings:
- `episodeId` = `{drama_slug}-ep-{ep}`
- `playUrl` = persisted `play_url`
- `durationMs` = persisted `duration_ms`
- `coverUrl` = persisted `cover_url`
- `drm.keyUri` = persisted `key_uri`
- `drm.keyBase64` = persisted `key_b64`
- `drm.ivHex` = persisted `iv_hex`

`initUrl`, `firstSegUrl`, and `fallback` SHALL be omitted (Phase 2). Any other row status — `pending`, `encoding`, `failed` — or missing row SHALL respond 404.

#### Scenario: Ready episode returns schema-conformant JSON
- **GIVEN** a row with `(drama_slug=langyabang, ep_number=3, status=ready)`
- **WHEN** the client requests `GET /api/episodes/langyabang/3`
- **THEN** the response is 200 JSON with at minimum `episodeId`, `playUrl`, `durationMs`, `coverUrl`, and a non-null `drm` object containing `keyUri`, `keyBase64`, `ivHex`
- **AND** the payload validates against `episode-info-schema.json`

#### Scenario: Non-ready episode is hidden from SDK
- **GIVEN** a row with `(drama_slug=langyabang, ep_number=3, status=encoding)`
- **WHEN** the client requests `GET /api/episodes/langyabang/3`
- **THEN** the response is 404

### Requirement: DRM key endpoint

The service SHALL serve `GET /drm/{drama_slug}/{ep}/key` returning the exact 16-byte binary contents of `OUT_DIR/{drama_slug}/keys/ep-{ep}.key` with `Content-Type: application/octet-stream`. The URL format SHALL match exactly what the worker passes as `<key_uri_base>` and what is written into `#EXT-X-KEY:URI` in the media playlist.

#### Scenario: Key fetch returns 16 bytes
- **GIVEN** the pipeline has produced `OUT_DIR/langyabang/keys/ep-3.key`
- **WHEN** the client requests `GET /drm/langyabang/3/key`
- **THEN** the response is 200, `Content-Length: 16`, `Content-Type: application/octet-stream`
- **AND** the body equals the file bytes

#### Scenario: Missing key returns 404
- **WHEN** the client requests `GET /drm/nonexistent/1/key`
- **THEN** the response is 404

### Requirement: Cover replacement endpoint

The service SHALL serve `POST /api/episodes/{drama_slug}/{ep}/cover` accepting `multipart/form-data` with a single `cover` file part whose MIME type starts with `image/`. The uploaded image SHALL overwrite `OUT_DIR/{drama_slug}/ep-{ep}/cover.jpg` on disk. The DB row's `updated_at` SHALL be bumped; `cover_url` SHALL remain unchanged (the URL is stable).

#### Scenario: Cover overwrite succeeds
- **GIVEN** a row with `(drama_slug=langyabang, ep_number=3)`
- **WHEN** the client posts a JPEG to `POST /api/episodes/langyabang/3/cover`
- **THEN** the file at `OUT_DIR/langyabang/ep-3/cover.jpg` is replaced with the uploaded bytes
- **AND** the row's `updated_at` is set to the current time

#### Scenario: Non-image payload is rejected
- **WHEN** the client posts `application/pdf` to the cover endpoint
- **THEN** the response is 400 and no file on disk is modified

### Requirement: Admin list endpoint

The service SHALL serve `GET /admin/episodes` returning a JSON array of every row ordered by `created_at` descending. Each element SHALL include `drama_slug`, `drama_name`, `ep_number`, `episode_id`, `status`, `duration_ms`, `play_url`, `cover_url`, `error_message`, `created_at`, `updated_at`. The endpoint SHALL NOT filter by status and SHALL NOT paginate.

#### Scenario: List returns every row
- **GIVEN** three rows with statuses `ready`, `encoding`, `failed`
- **WHEN** the client requests `GET /admin/episodes`
- **THEN** the response is 200 JSON containing all three rows
- **AND** the `failed` row carries a non-empty `error_message`

### Requirement: Admin web page

The service SHALL serve `GET /admin` returning an HTML page rendered against the shared admin base layout (see the `admin-redesign` capability's "shared admin layout and navigation" requirement). The page SHALL display a grid of drama cards as defined by the `admin-redesign` capability's "drama cards homepage" requirement, plus a "+ 创建短剧" call-to-action linking to `/admin/dramas/new`.

The page SHALL NOT contain a free-text upload form. Episode uploads now happen on the per-drama detail page (`/admin/dramas/{slug}`) via the auto-increment endpoint, and on the per-episode detail page for re-uploads.

The legacy two-form layout (drama-create + episode-upload + flat episode list) introduced in `drama-as-entity` is replaced wholesale by this new layout.

#### Scenario: Admin page is the drama cards homepage
- **WHEN** the client requests `GET /admin`
- **THEN** the response is 200 HTML extending the shared admin base layout
- **AND** the body contains drama cards (one per drama) and a "+ 创建短剧" link to `/admin/dramas/new`
- **AND** the body does NOT contain a `<form action="/admin/upload">` element

#### Scenario: Root redirects to admin
- **WHEN** the client requests `GET /`
- **THEN** the response is a 302/307 redirect to `/admin`

### Requirement: Static hosting of pipeline artifacts

The service SHALL static-mount `OUT_DIR/{drama_slug}/{ep}/` under `/videos/{drama_slug}/{ep}/` so that `.m3u8`, `.m4s`, `init-*.mp4`, and `cover.jpg` are directly fetchable. Responses SHALL include `Access-Control-Allow-Origin: *`. The mount SHALL NOT expose the sibling `keys/` directory.

#### Scenario: Playlist and segment are reachable
- **WHEN** the client requests `GET /videos/langyabang/ep-3/720p/media-720p.m3u8`
- **THEN** the response is 200 with the playlist bytes and `Access-Control-Allow-Origin: *`

#### Scenario: Key files are not exposed via /videos
- **WHEN** the client requests `GET /videos/langyabang/keys/ep-3.key`
- **THEN** the response is 404

### Requirement: Configuration via environment

The service SHALL read these environment variables at startup: `PUBLIC_BASE_URL` (required; fail fast if missing or not an absolute http(s) URL), `OUT_DIR` (default `./out`), `DB_PATH` (default `./hls.db`), `UPLOAD_TMP_DIR` (default `./tmp`). `PUBLIC_BASE_URL` SHALL be used verbatim (without normalization) when composing `playUrl`, `coverUrl`, and `key_uri` — so a trailing slash would produce a double-slash URL; the service SHALL strip exactly one trailing slash on load to avoid that.

#### Scenario: Missing PUBLIC_BASE_URL fails startup
- **WHEN** the process starts without `PUBLIC_BASE_URL` set
- **THEN** the process exits non-zero with a clear error message

#### Scenario: Trailing slash is tolerated
- **GIVEN** `PUBLIC_BASE_URL=http://hls.internal:8000/`
- **WHEN** the service composes a `playUrl`
- **THEN** the URL has exactly one slash between the base and `/videos/...`

### Requirement: EpisodeInfo schema cover field

The repository-level JSON Schema `episode-info-schema.json` SHALL include an optional property `coverUrl` of type `string | null`, `format: uri`, describing the episode cover image URL (first-frame JPEG by default, replaceable by admin). It SHALL NOT be added to `required`. This preserves backwards compatibility with existing SDK consumers.

#### Scenario: Existing valid payload without coverUrl still validates
- **GIVEN** an existing `EpisodeInfo` JSON that omits `coverUrl`
- **WHEN** it is validated against the updated schema
- **THEN** validation passes

#### Scenario: New payload with coverUrl validates
- **GIVEN** an `EpisodeInfo` payload that includes `coverUrl: "https://hls.internal:8000/videos/langyabang/ep-3/cover.jpg"`
- **WHEN** it is validated against the updated schema
- **THEN** validation passes
