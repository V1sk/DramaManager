# drama-entity

剧 (drama) 作为一等实体：独立的 `dramas` 表 + CRUD 端点 + 与 `episodes` 的外键约束。归档自 `drama-as-entity`。

## Requirements

### Requirement: dramas table schema

The service SHALL persist each drama in a SQLite table named `dramas` with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `default_lang` (TEXT NOT NULL, FOREIGN KEY → `languages(code)` ON DELETE RESTRICT), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC), `sync_status` (TEXT NOT NULL DEFAULT `'dirty'`), `sync_error` (TEXT, nullable), `last_synced_at` (TEXT, nullable, ISO 8601 UTC).

The table SHALL NOT carry a `name`, `synopsis`, or `poster_url` column. Those fields are stored in the `translations` table under `entity_type='drama'`.

`sync_status` value MUST be one of: `dirty`, `syncing`, `clean`, `sync_failed`, `pending_delete`. New rows default to `dirty` (the row hasn't been pushed to prod yet).

The `episodes` table SHALL have a foreign key constraint `FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT`. SQLite foreign keys SHALL be enabled at every connection.

#### Scenario: creating an episode for an unknown drama is rejected at the DB layer
- **GIVEN** the `dramas` table contains no row with `slug='ghost'`
- **WHEN** the application attempts `INSERT INTO episodes(drama_slug, ...) VALUES ('ghost', ...)`
- **THEN** SQLite raises `IntegrityError`

#### Scenario: deleting a drama with episodes is rejected at the DB layer
- **GIVEN** `dramas` has `slug='ly'` and `episodes` has at least one row with `drama_slug='ly'`
- **WHEN** the application attempts `DELETE FROM dramas WHERE slug='ly'`
- **THEN** SQLite raises `IntegrityError` (RESTRICT violation)

#### Scenario: dramas schema includes sync columns
- **WHEN** `init_db()` runs
- **THEN** `PRAGMA table_info(dramas)` lists exactly `slug`, `default_lang`, `created_at`, `updated_at`, `sync_status`, `sync_error`, `last_synced_at`

#### Scenario: new drama defaults to dirty
- **GIVEN** an empty database with one active language
- **WHEN** the operator POSTs `/admin/dramas` with valid fields
- **THEN** the resulting drama row has `sync_status='dirty'` and `last_synced_at IS NULL`

### Requirement: drama creation endpoint

The service SHALL provide `POST /admin/dramas` accepting `application/x-www-form-urlencoded` or `multipart/form-data` with fields: `drama_slug` (required), `drama_name` (required), `default_lang` (required).

The service SHALL validate `drama_slug` against `^[a-z0-9][a-z0-9-]*$`, `drama_name` is non-empty after trim, and `default_lang` is the `code` of an existing `languages` row. Any failure SHALL respond 400 with a message naming the offending field, and no row SHALL be inserted in either `dramas` or `translations`.

If a drama with the given slug already exists, the service SHALL respond 409 Conflict.

On success the service SHALL atomically:
1. Insert the `dramas` row with `created_at = updated_at = now`.
2. Insert the translation row `(entity_type='drama', entity_id=drama_slug, lang_code=default_lang, field='name', value=drama_name)`.

Both inserts SHALL share a transaction. On failure either both rows exist or neither. The response SHALL be HTTP 302 redirect to `/admin` (form-style).

#### Scenario: valid drama creation succeeds
- **GIVEN** `languages` has active `('zh-rCN', '简体中文', 1)`
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN`
- **THEN** the `dramas` table has the row with `(slug='ly', default_lang='zh-rCN')`
- **AND** the `translations` table has the row `('drama', 'ly', 'zh-rCN', 'name', '琅琊榜')`
- **AND** the response is 302 to `/admin`

#### Scenario: duplicate slug is rejected
- **GIVEN** a `dramas` row with `slug='ly'` already exists
- **WHEN** a client posts the same slug
- **THEN** the response is 409
- **AND** no new row is inserted in either table

#### Scenario: unknown default_lang is rejected
- **GIVEN** `languages` has no row with `code='zz'`
- **WHEN** a client posts `drama_slug=ok`, `drama_name=OK`, `default_lang=zz`
- **THEN** the response is 400 naming `default_lang`
- **AND** no row is inserted in either table

#### Scenario: empty drama_name is rejected
- **WHEN** a client posts `drama_slug=ok`, `drama_name=   `, `default_lang=en`
- **THEN** the response is 400 naming `drama_name`
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

The service SHALL provide `DELETE /admin/dramas/{slug}` (slug pattern same as elsewhere; 422 otherwise).

If the drama row does not exist → 404.

If the drama row exists and `episodes` has at least one row with `drama_slug={slug}` → 409 ("delete episodes first"). The row SHALL NOT be modified.

If the row exists and `episodes` has zero rows, the handler SHALL branch on `last_synced_at`:

**Branch A — never synced** (`last_synced_at IS NULL`):
1. Delete the row from `dramas`.
2. Remove `OUT_DIR/{slug}/` (entire subtree).
3. When `settings.oss_enabled`, call `unpublish_drama_from_staging(slug)`.
4. Also `DELETE FROM translations WHERE entity_type='drama' AND entity_id=slug` (covers covers/posters/synopsis translations).
5. Response: `200 {"ok": true, "warnings": [...]}`.

**Branch B — previously synced** (`last_synced_at IS NOT NULL`):
1. Update the drama row to `sync_status='pending_delete'` and refresh `updated_at`. **The row stays.**
2. Remove `OUT_DIR/{slug}/` (entire subtree).
3. When `settings.oss_enabled`, call `unpublish_drama_from_staging(slug)`.
4. Also `DELETE FROM translations WHERE entity_type='drama' AND entity_id=slug`.
5. Response: `200 {"ok": true, "warnings": [...], "pending_sync": true}`.
6. The row is physically removed only after a successful `DELETE /sync/dramas/{slug}` propagated by the sync worker (per the `business-server-sync` capability).

Disk-removal failures MUST NOT roll back the DB state; warnings SHALL be logged and surfaced in the response.

#### Scenario: deleting a never-synced empty drama physically removes it
- **GIVEN** `dramas` has `slug='gone'` with `last_synced_at IS NULL`, zero episodes, and `OUT_DIR/gone/` exists
- **WHEN** the client requests `DELETE /admin/dramas/gone`
- **THEN** the response is `200 {"ok": true, ...}` without `pending_sync`
- **AND** the `dramas` row is gone
- **AND** `OUT_DIR/gone/` does not exist

#### Scenario: deleting a previously-synced empty drama marks it pending_delete
- **GIVEN** `dramas` has `slug='gone'` with `last_synced_at` set, zero episodes
- **WHEN** the client requests `DELETE /admin/dramas/gone`
- **THEN** the response is `200 {"ok": true, ..., "pending_sync": true}`
- **AND** the `dramas` row still exists with `sync_status='pending_delete'`
- **AND** `OUT_DIR/gone/` is removed
- **AND** the row's translations are removed

#### Scenario: deleting a non-empty drama is rejected
- **GIVEN** `dramas` has `slug='ly'` and `episodes` has at least one row with `drama_slug='ly'`
- **WHEN** the client requests `DELETE /admin/dramas/ly`
- **THEN** the response is 409
- **AND** the row is unchanged

### Requirement: drama default-lang update endpoint

The service SHALL provide `PATCH /admin/dramas/{slug}` accepting JSON body with at most one field: `default_lang`. Other payload fields SHALL be rejected with 400.

The new `default_lang` MUST reference an active language AND a translation row MUST already exist for `(entity_type='drama', entity_id=slug, lang_code=new_default, field='name')`. If either fails the response is 400 naming the offending condition.

If the drama is unknown the response is 404. On success the row's `default_lang` is updated, `updated_at` refreshed, and the response is 200 with the updated row.

#### Scenario: switching default to a covered language succeeds
- **GIVEN** drama `ly` with `default_lang='zh-rCN'` and translation rows `(drama, ly, en, name)` already populated
- **WHEN** the client sends `PATCH /admin/dramas/ly` with body `{"default_lang": "en"}`
- **THEN** the response is 200
- **AND** `dramas.ly.default_lang` is now `'en'`

#### Scenario: switching default to an uncovered language is rejected
- **GIVEN** drama `ly` with no `name` translation in `ja`
- **WHEN** the client sends `PATCH /admin/dramas/ly` with body `{"default_lang": "ja"}`
- **THEN** the response is 400
- **AND** the row is unchanged

### Requirement: drama deletion cleans translations and posters

The existing `DELETE /admin/dramas/{slug}` endpoint (defined in the `drama-entity` capability) SHALL additionally delete every row in `translations` with `entity_type='drama' AND entity_id=slug` before removing the drama row. The on-disk subtree under `OUT_DIR/{slug}/` (which contains `poster/`) is removed by the existing `shutil.rmtree` step; this requirement only adds the translations cleanup.

#### Scenario: drama deletion removes translation rows
- **GIVEN** drama `ly` with translations in `zh-rCN` and `en` (name + synopsis + poster)
- **WHEN** the client requests `DELETE /admin/dramas/ly` and the drama has no episodes
- **THEN** the response is 200
- **AND** zero rows remain in `translations` with `(entity_type='drama', entity_id='ly')`
- **AND** the `OUT_DIR/ly/` subtree is gone (including `poster/`)

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
