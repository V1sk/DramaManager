## MODIFIED Requirements

### Requirement: Synchronous metadata extraction

Before persisting the episode row the service SHALL, in order: (1) probe the uploaded video with `ffprobe` to obtain `duration_ms` as an integer number of milliseconds; (2) extract the first frame of the uploaded video with `ffmpeg` (`-ss 0 -vframes 1 -vf scale=-2:720`) to `OUT_DIR/{drama_slug}/ep-{n}/cover.jpg`; (3) **when `settings.oss_enabled`**: upload the just-extracted cover to OSS staging via `publish.upload_cover_to_staging(drama_slug, ep_dir, local_path)`. All three steps run before the HTTP response returns.

If step 3 fails the upload handler SHALL roll back step 2 (unlink the local cover file) and respond 500. The temporary uploaded video MUST be deleted in either path (success or failure).

#### Scenario: Duration, cover, and OSS staging mirror are produced before response
- **GIVEN** OSS mode enabled, valid upload to `POST /admin/dramas/ly/episodes`
- **WHEN** the upload completes
- **THEN** `OUT_DIR/ly/ep-3/cover.jpg` exists on disk
- **AND** OSS object `Drama/staging/ly/ep-3/cover.jpg` exists with the same bytes
- **AND** the DB row carries a non-null `duration_ms` and a non-null `cover_url`
- **BEFORE** the 302 response returns to the client

#### Scenario: cover extraction with OSS disabled stays local-only
- **GIVEN** OSS mode disabled, valid upload
- **WHEN** the upload completes
- **THEN** `OUT_DIR/.../cover.jpg` exists on disk
- **AND** no OSS upload is attempted
- **AND** the DB row is persisted normally

#### Scenario: Cover OSS upload failure rolls back upload handler
- **GIVEN** OSS mode enabled, OSS service unreachable
- **WHEN** the operator POSTs a video upload
- **THEN** the response is 500
- **AND** no `OUT_DIR/.../cover.jpg` remains on disk (rolled back)
- **AND** no `episodes` row was inserted
- **AND** the temporary uploaded video is deleted

#### Scenario: ffprobe failure aborts the upload
- **WHEN** `ffprobe` exits non-zero against the uploaded file
- **THEN** the service responds 400 and stores nothing in the DB
- **AND** the temporary upload file is deleted

### Requirement: Cover replacement endpoint

The service SHALL serve `POST /api/episodes/{drama_slug}/{ep}/cover` accepting `multipart/form-data` with a single `cover` file part whose MIME type starts with `image/`. On success the handler SHALL:
1. Overwrite `OUT_DIR/{drama_slug}/ep-{ep}/cover.jpg` on disk with the uploaded bytes.
2. **When `settings.oss_enabled`**: call `publish.upload_cover_to_staging(drama_slug, ep_dir, local_path)` to mirror the new bytes to OSS staging. On OSS failure, the handler MUST attempt to restore the prior `cover.jpg` content (best-effort — if a backup snapshot was taken before overwrite) or unlink the file; in either case respond 500. Local-only deploys (OSS disabled) skip this step.
3. Bump the row's `updated_at`; `cover_url` SHALL remain unchanged (the URL is stable).

#### Scenario: cover overwrite mirrors to OSS staging
- **GIVEN** OSS mode enabled, episode `(ly, 3)` has a prior cover locally and at staging OSS
- **WHEN** the client posts a new JPEG to `POST /api/episodes/ly/3/cover`
- **THEN** the file at `OUT_DIR/ly/ep-3/cover.jpg` is replaced with the uploaded bytes
- **AND** OSS object `Drama/staging/ly/ep-3/cover.jpg` now has the new bytes
- **AND** the row's `updated_at` is set to the current time

#### Scenario: cover overwrite with OSS disabled stays local-only
- **GIVEN** OSS mode disabled, episode `(ly, 3)`
- **WHEN** the client posts a JPEG to the cover endpoint
- **THEN** the local file is replaced
- **AND** no OSS upload is attempted

#### Scenario: cover overwrite OSS failure responds 500
- **GIVEN** OSS mode enabled, OSS unreachable
- **WHEN** the client posts a JPEG to `POST /api/episodes/ly/3/cover`
- **THEN** the response is 500
- **AND** the row's `updated_at` is NOT bumped

#### Scenario: Non-image payload is rejected
- **WHEN** the client posts `application/pdf` to the cover endpoint
- **THEN** the response is 400 and no file on disk is modified
- **AND** no OSS upload is attempted
