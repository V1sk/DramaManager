## ADDED Requirements

### Requirement: dramas table schema

The service SHALL persist each drama in a SQLite table named `dramas` with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `name` (TEXT NOT NULL, non-empty after trim), `default_lang` (TEXT NOT NULL, matches `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

The `episodes` table SHALL have a foreign key constraint `FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT`. SQLite foreign keys SHALL be enabled at every connection (`PRAGMA foreign_keys = ON`) so the constraint is enforced.

#### Scenario: creating an episode for an unknown drama is rejected at the DB layer
- **GIVEN** the `dramas` table contains no row with `slug='ghost'`
- **WHEN** the application attempts `INSERT INTO episodes(drama_slug, ...) VALUES ('ghost', ...)`
- **THEN** SQLite raises `IntegrityError`
- **AND** no row is inserted into `episodes`

#### Scenario: deleting a drama with episodes is rejected at the DB layer
- **GIVEN** `dramas` has a row with `slug='ly'` and `episodes` has at least one row with `drama_slug='ly'`
- **WHEN** the application attempts `DELETE FROM dramas WHERE slug='ly'`
- **THEN** SQLite raises `IntegrityError` (RESTRICT violation)
- **AND** the drama row remains

### Requirement: drama creation endpoint

The service SHALL provide `POST /admin/dramas` accepting `application/x-www-form-urlencoded` or `multipart/form-data` with fields: `drama_slug` (required), `drama_name` (required), `default_lang` (required).

The service SHALL validate `drama_slug` against `^[a-z0-9][a-z0-9-]*$`, `drama_name` is non-empty after trim, and `default_lang` against `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`. Any failure SHALL respond 400 with a message naming the offending field, and no row SHALL be inserted.

If a drama with the given slug already exists, the service SHALL respond 409 Conflict with a message indicating the slug is taken; no row SHALL be modified.

On success the service SHALL insert a new `dramas` row with `created_at = updated_at = now (ISO 8601 UTC)`. The response SHALL be HTTP 302 redirect to `/admin` (form-style) for browser submissions; programmatic clients can rely on a successful 2xx/3xx and the row's existence.

#### Scenario: valid drama creation succeeds
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN` to `POST /admin/dramas`
- **THEN** a row exists in `dramas` with those values
- **AND** the response is 302 to `/admin`

#### Scenario: duplicate slug is rejected
- **GIVEN** a `dramas` row with `slug='ly'` already exists
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN`
- **THEN** the response is 409
- **AND** the existing row is unchanged

#### Scenario: invalid default_lang is rejected
- **WHEN** a client posts `drama_slug=ok`, `drama_name=OK`, `default_lang=Chinese!`
- **THEN** the response is 400 naming the `default_lang` field
- **AND** no row is inserted

#### Scenario: empty drama_name is rejected
- **WHEN** a client posts `drama_slug=ok`, `drama_name=   `, `default_lang=en`
- **THEN** the response is 400 naming the `drama_name` field
- **AND** no row is inserted

### Requirement: drama listing endpoint

The service SHALL provide `GET /admin/dramas` returning a JSON array of every drama row joined with the count of its episodes. Each element SHALL include `slug`, `name`, `default_lang`, `ep_count` (integer ≥ 0, may be 0 for newly-created drama), `created_at`, `updated_at`. Ordering SHALL be `created_at DESC` (newest first); two rows with identical `created_at` SHALL be ordered by `slug ASC` for stability.

#### Scenario: list returns all dramas with episode counts
- **GIVEN** two dramas: `slug='a'` with 2 episodes, `slug='b'` with 0 episodes
- **WHEN** the client requests `GET /admin/dramas`
- **THEN** the response is 200 JSON containing both dramas
- **AND** the `a` element has `ep_count=2`, `b` has `ep_count=0`

#### Scenario: empty database returns empty array
- **GIVEN** the `dramas` table is empty
- **WHEN** the client requests `GET /admin/dramas`
- **THEN** the response is 200 with body `[]`

### Requirement: drama deletion endpoint

The service SHALL provide `DELETE /admin/dramas/{slug}` where `{slug}` matches `^[a-z0-9][a-z0-9-]*$` (otherwise 422).

When the row exists and `episodes` has zero rows with `drama_slug={slug}`, the service SHALL:
1. Delete the row from `dramas`.
2. Remove `OUT_DIR/{slug}/` (entire subtree, including any residual `keys/` directory). `shutil.rmtree` MUST tolerate the directory not existing.

When the row exists but `episodes` has at least one row with `drama_slug={slug}`, the service SHALL respond 409 Conflict with a message that explains episodes must be deleted first; the row and disk artifacts SHALL be unchanged.

When no row matches the slug, the service SHALL respond 404 with a message and not touch disk.

Disk-removal failures MUST NOT roll back the DB delete; warnings SHALL be logged at WARNING level and surfaced in the response body's `warnings` array. The success response is `200 {"ok": true, "warnings": [...]}`.

#### Scenario: deleting an empty drama removes the row and the on-disk directory
- **GIVEN** `dramas` has `slug='gone'`, `episodes` has zero rows for it, `OUT_DIR/gone/` exists (perhaps still has a stale `keys/` dir)
- **WHEN** the client requests `DELETE /admin/dramas/gone`
- **THEN** the response is 200 `{"ok": true, "warnings": []}`
- **AND** the `dramas` row is gone
- **AND** `OUT_DIR/gone/` does not exist

#### Scenario: deleting a non-empty drama is rejected
- **GIVEN** `dramas` has `slug='ly'` and `episodes` has at least one row with `drama_slug='ly'`
- **WHEN** the client requests `DELETE /admin/dramas/ly`
- **THEN** the response is 409
- **AND** the `dramas` row is unchanged
- **AND** `OUT_DIR/ly/` is unchanged

#### Scenario: deleting an unknown slug returns 404
- **WHEN** the client requests `DELETE /admin/dramas/never-seen`
- **THEN** the response is 404
- **AND** disk is not touched

### Requirement: episode upload precondition

`POST /admin/upload` SHALL reject any request whose `drama_slug` does not match an existing `dramas` row. The rejection SHALL be HTTP 400 with a message naming the slug and pointing the operator to `POST /admin/dramas`. No row SHALL be created or modified in the `episodes` table; the temporary upload file (if any was streamed to disk) SHALL be deleted before the response returns.

#### Scenario: upload to non-existent drama is rejected
- **GIVEN** the `dramas` table contains no row with `slug='nodrama'`
- **WHEN** a client posts a valid video to `POST /admin/upload` with `drama_slug=nodrama`, `ep_number=1`
- **THEN** the response is 400 with a message that names `nodrama`
- **AND** no row is inserted into `episodes`
- **AND** the streamed temporary file under `UPLOAD_TMP_DIR` does not remain on disk

#### Scenario: upload to existing drama proceeds normally
- **GIVEN** `dramas` has `slug='ly'`
- **WHEN** a client posts a valid video to `POST /admin/upload` with `drama_slug=ly`, `ep_number=1`
- **THEN** the existing upload pipeline (ffprobe, cover extraction, persist row, enqueue) executes
- **AND** the response is 302 to `/admin`

### Requirement: drama-name sourcing for downstream readers

All API endpoints that surface a drama name (`GET /admin/episodes`, `GET /api/dramas`, `GET /api/dramas/{slug}/episodes`, `GET /api/episodes/{slug}/{ep}`) SHALL source the value from `dramas.name` via a JOIN, never from a column on `episodes`. The shape of these responses (field names, types, nullability) MUST NOT change.

#### Scenario: renaming the drama is reflected in admin episode list
- **GIVEN** `dramas` has `slug='ly'`, `name='琅琊榜'` and `episodes` has rows for it
- **WHEN** the operator updates `dramas.name` to `'琅琊榜·重制版'` (via direct DB edit or future endpoint)
- **AND** the client requests `GET /admin/episodes`
- **THEN** every row with `drama_slug='ly'` shows `drama_name='琅琊榜·重制版'`

#### Scenario: SDK drama summary uses the drama row's name
- **GIVEN** `dramas` has `slug='ly'`, `name='琅琊榜'`, and at least one ready episode
- **WHEN** the client requests `GET /api/dramas`
- **THEN** the corresponding element's `dramaName` is `'琅琊榜'`
