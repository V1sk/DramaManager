## MODIFIED Requirements

### Requirement: Upload intake

The service SHALL accept an HTTP `POST /admin/upload` request with `multipart/form-data` carrying the fields `video` (required file), `drama_slug` (required text), and `ep_number` (required integer ≥ 1). It SHALL reject requests whose `drama_slug` does not match `^[a-z0-9][a-z0-9-]*$`, whose `ep_number` is missing or non-positive, or whose `video` part is missing. It SHALL also reject any request whose `drama_slug` does not match an existing row in the `dramas` table — see the `drama-entity` capability for the precondition. The field `drama_name` is no longer accepted on this endpoint; the drama name is sourced from the `dramas` row.

#### Scenario: Valid upload is accepted
- **GIVEN** the `dramas` table has a row with `slug='langyabang'`
- **WHEN** a client posts `video=<mp4>`, `drama_slug=langyabang`, `ep_number=3`
- **THEN** the service writes the upload to a temporary file under `UPLOAD_TMP_DIR`
- **AND** responds with an HTTP 302 redirect to `/admin` once the row exists and the job is enqueued

#### Scenario: Malformed drama_slug is rejected
- **WHEN** a client posts `drama_slug=Langya Bang` (contains uppercase and whitespace)
- **THEN** the service responds 400 with a message naming the `drama_slug` field
- **AND** no file is written under `OUT_DIR` and no DB row is created

#### Scenario: Missing video file is rejected
- **WHEN** a client posts the form without a `video` part
- **THEN** the service responds 400 with a message naming the `video` field
- **AND** no DB row is created

#### Scenario: drama_name field is ignored when present
- **GIVEN** the `dramas` table has a row with `slug='ly'`, `name='琅琊榜'`
- **WHEN** a client posts `video=<mp4>`, `drama_slug=ly`, `ep_number=1`, `drama_name='unrelated'` (an extra field)
- **THEN** the service either ignores the field silently or responds 400 — but in either case the drama row's `name` is unchanged
- **AND** the resulting episode row has no `drama_name` column to populate

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

### Requirement: Admin web page

The service SHALL serve `GET /admin` returning an HTML page containing:
1. A "create drama" form with fields: `drama_slug`, `drama_name`, `default_lang`. Submitting it POSTs to `/admin/dramas`.
2. An "upload episode" form with fields: `drama_slug`, `ep_number`, `video file`. Submitting it POSTs to `/admin/upload`. The form SHALL NOT include a `drama_name` field.
3. An episode list. Each entry SHALL show the cover as a thumbnail, the drama name (sourced via JOIN from `dramas`), episode number, status, `duration_ms` (when present), `created_at`, and — when status is `failed` — the `error_message`. Clicking the cover SHALL open a file picker that POSTs to `/api/episodes/{slug}/{ep}/cover`. Each entry SHALL include a delete button per the `episode-deletion` capability.

The page MAY also list dramas (sourced from `GET /admin/dramas`) — display is optional in this change; the next change (`admin-redesign`) replaces this page wholesale.

#### Scenario: Admin page loads with both forms
- **WHEN** the client requests `GET /admin`
- **THEN** the response is 200 HTML that contains a form posting to `/admin/dramas` and a form posting to `/admin/upload`
- **AND** the upload form has no `drama_name` input

#### Scenario: Root redirects to admin
- **WHEN** the client requests `GET /`
- **THEN** the response is a 302/307 redirect to `/admin`
