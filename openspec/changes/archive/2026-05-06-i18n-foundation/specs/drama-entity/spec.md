## MODIFIED Requirements

### Requirement: dramas table schema

The service SHALL persist each drama in a SQLite table named `dramas` with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `name` (TEXT NOT NULL, non-empty after trim), `default_lang` (TEXT NOT NULL), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

The `dramas.default_lang` column SHALL be a FOREIGN KEY onto `languages(code)` with `ON DELETE RESTRICT`. Validation that the chosen `default_lang` exists in `languages` AND is `is_active=1` is performed at the application layer at creation time (the `default_lang` regex check is removed; the FK is now the source of truth).

The `episodes` table SHALL have a foreign key constraint `FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT`. SQLite foreign keys SHALL be enabled at every connection (`PRAGMA foreign_keys = ON`) so both constraints are enforced.

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

#### Scenario: creating a drama with an unknown default_lang is rejected at the DB layer
- **GIVEN** the `languages` table contains no row with `code='zz'`
- **WHEN** the application attempts `INSERT INTO dramas(slug, name, default_lang, ...) VALUES ('ly', '琅琊榜', 'zz', ...)`
- **THEN** SQLite raises `IntegrityError`
- **AND** no row is inserted

#### Scenario: deleting a language referenced as a drama default is rejected at the DB layer
- **GIVEN** `languages` has `code='en'` and `dramas` has at least one row with `default_lang='en'`
- **WHEN** the application attempts `DELETE FROM languages WHERE code='en'`
- **THEN** SQLite raises `IntegrityError`
- **AND** the language row remains

### Requirement: drama creation endpoint

The service SHALL provide `POST /admin/dramas` accepting `application/x-www-form-urlencoded` or `multipart/form-data` with fields: `drama_slug` (required), `drama_name` (required), `default_lang` (required).

The service SHALL validate `drama_slug` against `^[a-z0-9][a-z0-9-]*$`, `drama_name` is non-empty after trim, and `default_lang` is the `code` of an existing `languages` row whose `is_active=1`. Any failure SHALL respond 400 with a message naming the offending field, and no row SHALL be inserted.

If a drama with the given slug already exists, the service SHALL respond 409 Conflict with a message indicating the slug is taken; no row SHALL be modified.

On success the service SHALL insert a new `dramas` row with `created_at = updated_at = now (ISO 8601 UTC)`. The response SHALL be HTTP 302 redirect to `/admin` (form-style) for browser submissions; programmatic clients can rely on a successful 2xx/3xx and the row's existence.

#### Scenario: valid drama creation succeeds
- **GIVEN** `languages` has a row `('zh-rCN', '简体中文', 1)`
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN` to `POST /admin/dramas`
- **THEN** a row exists in `dramas` with those values
- **AND** the response is 302 to `/admin`

#### Scenario: duplicate slug is rejected
- **GIVEN** a `dramas` row with `slug='ly'` already exists
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN`
- **THEN** the response is 409
- **AND** the existing row is unchanged

#### Scenario: unknown default_lang is rejected
- **GIVEN** `languages` has no row with `code='zz'`
- **WHEN** a client posts `drama_slug=ok`, `drama_name=OK`, `default_lang=zz`
- **THEN** the response is 400 naming the `default_lang` field
- **AND** no row is inserted

#### Scenario: inactive default_lang is rejected
- **GIVEN** `languages` has `('ja', 'Japanese', 0)` (is_active=false)
- **WHEN** a client posts `drama_slug=ok`, `drama_name=OK`, `default_lang=ja`
- **THEN** the response is 400 naming the `default_lang` field
- **AND** no row is inserted

#### Scenario: empty drama_name is rejected
- **WHEN** a client posts `drama_slug=ok`, `drama_name=   `, `default_lang=en`
- **THEN** the response is 400 naming the `drama_name` field
- **AND** no row is inserted
