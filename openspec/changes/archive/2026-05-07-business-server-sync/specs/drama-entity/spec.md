## MODIFIED Requirements

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
