## MODIFIED Requirements

### Requirement: dramas table schema

The service SHALL persist each drama in a SQLite table named `dramas` with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `default_lang` (TEXT NOT NULL, FOREIGN KEY → `languages(code)` ON DELETE RESTRICT), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

The table SHALL NOT carry a `name`, `synopsis`, or `poster_url` column. Those fields are stored in the `translations` table under `entity_type='drama'`.

The `episodes` table SHALL have a foreign key constraint `FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT`. SQLite foreign keys SHALL be enabled at every connection (`PRAGMA foreign_keys = ON`).

#### Scenario: creating an episode for an unknown drama is rejected at the DB layer
- **GIVEN** the `dramas` table contains no row with `slug='ghost'`
- **WHEN** the application attempts `INSERT INTO episodes(drama_slug, ...) VALUES ('ghost', ...)`
- **THEN** SQLite raises `IntegrityError`

#### Scenario: deleting a drama with episodes is rejected at the DB layer
- **GIVEN** `dramas` has `slug='ly'` and `episodes` has at least one row with `drama_slug='ly'`
- **WHEN** the application attempts `DELETE FROM dramas WHERE slug='ly'`
- **THEN** SQLite raises `IntegrityError` (RESTRICT violation)

#### Scenario: dramas schema does not include name / synopsis / poster columns
- **WHEN** `init_db()` runs
- **THEN** `PRAGMA table_info(dramas)` lists exactly `slug`, `default_lang`, `created_at`, `updated_at`

### Requirement: drama creation endpoint

The service SHALL provide `POST /admin/dramas` accepting `application/x-www-form-urlencoded` or `multipart/form-data` with fields: `drama_slug` (required), `drama_name` (required), `default_lang` (required).

The service SHALL validate `drama_slug` against `^[a-z0-9][a-z0-9-]*$`, `drama_name` is non-empty after trim, and `default_lang` is the `code` of an existing `languages` row whose `is_active=1`. Any failure SHALL respond 400 with a message naming the offending field, and no row SHALL be inserted in either `dramas` or `translations`.

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

## ADDED Requirements

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
