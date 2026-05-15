# i18n-foundation

国际化基础设施：`languages` 与 `translations` 表 + 语言 CRUD 端点 + 管理页 + SDK 语言列表端点。归档自 `i18n-foundation`。

## Requirements

### Requirement: languages table schema

The service SHALL persist a `languages` table with columns: `code` (TEXT, PRIMARY KEY, matches `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`), `display_label` (TEXT NOT NULL, non-empty after trim), `is_active` (INTEGER NOT NULL DEFAULT 1, value 0 or 1), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

#### Scenario: schema is created on init
- **WHEN** the service starts with an empty DB and `init_db()` runs
- **THEN** `PRAGMA table_info(languages)` lists exactly the columns `code`, `display_label`, `is_active`, `created_at`, `updated_at`
- **AND** `code` is the primary key

#### Scenario: invalid code is rejected by the regex
- **WHEN** the application attempts `INSERT INTO languages(code, display_label, ...) VALUES ('Bad Lang!', '...', ...)`
- **THEN** the application rejects the value before it reaches SQLite (the validation lives in the create helper / endpoint)

### Requirement: translations table schema

The service SHALL persist a `translations` table with columns: `entity_type` (TEXT NOT NULL), `entity_id` (TEXT NOT NULL), `lang_code` (TEXT NOT NULL), `field` (TEXT NOT NULL), `value` (TEXT NOT NULL). The composite PRIMARY KEY SHALL be `(entity_type, entity_id, lang_code, field)`. The column `lang_code` SHALL be a FOREIGN KEY onto `languages(code)` with `ON DELETE RESTRICT`.

The table SHALL be created and FK-enforced even though no consumer writes to it in this change. Downstream changes populate it; the schema must be ready when they land.

#### Scenario: translations.lang_code FK is enforced
- **GIVEN** the `languages` table has no row with `code='zz'`
- **WHEN** the application attempts `INSERT INTO translations(entity_type, entity_id, lang_code, field, value) VALUES ('drama', 'ly', 'zz', 'name', '...')`
- **THEN** SQLite raises `IntegrityError`
- **AND** no row is inserted

#### Scenario: deleting a referenced language is rejected
- **GIVEN** `languages` has `code='en'` and `translations` has at least one row with `lang_code='en'`
- **WHEN** the application attempts `DELETE FROM languages WHERE code='en'`
- **THEN** SQLite raises `IntegrityError`
- **AND** the row remains

#### Scenario: composite PK prevents duplicate translation
- **GIVEN** a row exists in `translations` with `(entity_type='drama', entity_id='ly', lang_code='en', field='name', value='Langya Bang')`
- **WHEN** the application attempts a second `INSERT` with the same four key columns
- **THEN** SQLite raises `IntegrityError` (PRIMARY KEY collision)
- **AND** the existing row is unchanged unless the operation was explicitly an UPSERT (`INSERT … ON CONFLICT DO UPDATE`)

### Requirement: language creation endpoint

The service SHALL provide `POST /admin/languages` accepting form fields `code` (required) and `display_label` (required). It SHALL validate `code` against `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$` and `display_label` non-empty after trim. Either failure SHALL respond 400 naming the offending field; no row SHALL be inserted.

If a row with that code already exists the service SHALL respond 409 Conflict. Otherwise it SHALL insert a new row with `is_active=1`, `created_at = updated_at = now`, and respond HTTP 302 to `/admin/languages` (browser-form posture).

#### Scenario: valid creation succeeds
- **WHEN** a client posts `code=zh-rCN`, `display_label=简体中文` to `POST /admin/languages`
- **THEN** a row exists with `(code='zh-rCN', display_label='简体中文', is_active=1)`
- **AND** the response is 302 to `/admin/languages`

#### Scenario: duplicate code is rejected
- **GIVEN** `languages` has `code='en'`
- **WHEN** a client posts `code=en`, `display_label='English'`
- **THEN** the response is 409
- **AND** the existing row is unchanged

#### Scenario: invalid code is rejected
- **WHEN** a client posts `code=ENG`, `display_label='English'` (uppercase)
- **THEN** the response is 400 naming the `code` field
- **AND** no row is inserted

### Requirement: language listing endpoint (admin)

The service SHALL provide `GET /admin/languages` returning a JSON array of every row in the `languages` table — including rows with `is_active=0`. Each element SHALL include `code`, `display_label`, `is_active`, `created_at`, `updated_at`. Ordering SHALL be `code ASC`.

#### Scenario: list returns active and inactive rows
- **GIVEN** `languages` has rows `('en', 'English', 1)` and `('ja', 'Japanese', 0)`
- **WHEN** the client requests `GET /admin/languages`
- **THEN** the response is 200 JSON containing both rows
- **AND** ordering is `[en, ja]` (alphabetical)

### Requirement: language update endpoint

The service SHALL provide `PATCH /admin/languages/{code}` accepting a JSON body with optional fields `display_label` and `is_active`. Unknown fields SHALL be rejected with 400. Both fields, when present, SHALL be validated (`display_label` non-empty after trim, `is_active` is `true`/`false`/`1`/`0`).

The `code` itself SHALL NOT be updatable — the path parameter identifies the row and is the immutable key. Any payload field named `code` SHALL be rejected with 400.

If no row matches `{code}`, the service SHALL respond 404. Otherwise the row SHALL be updated, `updated_at` refreshed, and the response is 200 with the new row body.

#### Scenario: toggling is_active off
- **GIVEN** `languages` has `code='ja', is_active=1`
- **WHEN** the client sends `PATCH /admin/languages/ja` with body `{"is_active": false}`
- **THEN** the response is 200
- **AND** the row now has `is_active=0`
- **AND** `display_label` is unchanged

#### Scenario: updating display_label
- **GIVEN** `languages` has `code='zh-rCN', display_label='中文'`
- **WHEN** the client sends `PATCH /admin/languages/zh-rCN` with body `{"display_label": "简体中文"}`
- **THEN** the response is 200
- **AND** the row's `display_label` is `'简体中文'`

#### Scenario: payload that tries to change code is rejected
- **WHEN** the client sends `PATCH /admin/languages/en` with body `{"code": "en-US"}`
- **THEN** the response is 400
- **AND** the `en` row is unchanged

#### Scenario: unknown code returns 404
- **WHEN** the client sends `PATCH /admin/languages/xx` against a missing row
- **THEN** the response is 404

### Requirement: language deletion endpoint

The service SHALL provide `DELETE /admin/languages/{code}`. If no row matches the code, the service SHALL respond 404.

If at least one drama has `default_lang = {code}` OR at least one row exists in `translations` with `lang_code = {code}`, the service SHALL respond 409 Conflict with a body that names which kinds of references exist (e.g. `{"error": "language is referenced", "dramas": 3, "translations": 17}`). The row SHALL NOT be deleted.

Otherwise the row SHALL be deleted; the response is 204 No Content.

#### Scenario: deleting an unreferenced language succeeds
- **GIVEN** `languages` has `code='zz', display_label='Test'`, no drama has `default_lang='zz'`, no translation has `lang_code='zz'`
- **WHEN** the client requests `DELETE /admin/languages/zz`
- **THEN** the response is 204
- **AND** the row is gone

#### Scenario: deleting a language referenced by a drama default is rejected
- **GIVEN** `languages` has `code='en'`, `dramas` has at least one row with `default_lang='en'`
- **WHEN** the client requests `DELETE /admin/languages/en`
- **THEN** the response is 409 with a body indicating drama references
- **AND** the language row remains

#### Scenario: deleting a language referenced by translations is rejected
- **GIVEN** `languages` has `code='ja'`, `translations` has at least one row with `lang_code='ja'`
- **WHEN** the client requests `DELETE /admin/languages/ja`
- **THEN** the response is 409 with a body indicating translation references
- **AND** the language row remains

#### Scenario: unknown code returns 404
- **WHEN** the client requests `DELETE /admin/languages/never-seen`
- **THEN** the response is 404

### Requirement: SDK languages endpoint

The service SHALL provide `GET /api/languages` returning a JSON array of `{code, display_label}` objects for every row in `languages` with `is_active=1`. Inactive languages SHALL NOT appear. Ordering SHALL be `code ASC`. An empty registry SHALL return `200 []`.

The endpoint SHALL NOT require authentication (consistent with other `/api/*` endpoints) and SHALL set `Access-Control-Allow-Origin: *` (consistent with other `/api/*` endpoints, served by FastAPI's CORS middleware).

#### Scenario: only active rows are returned
- **GIVEN** `languages` has rows `('en', 'English', 1)` and `('ja', 'Japanese', 0)`
- **WHEN** the client requests `GET /api/languages`
- **THEN** the response is 200 with body `[{"code": "en", "display_label": "English"}]`

#### Scenario: empty registry returns []
- **GIVEN** the `languages` table is empty
- **WHEN** the client requests `GET /api/languages`
- **THEN** the response is 200 with body `[]`

### Requirement: minimal admin /languages page

The service SHALL serve `GET /admin/languages.html` (or extend `GET /admin` with a navigation link) returning an HTML page that contains:
1. A create form with fields `code` and `display_label` posting to `/admin/languages`.
2. A table listing every language (including `is_active=0` rows), each row showing `code`, `display_label`, the active flag, and two action buttons: toggle `is_active` (PATCH) and delete (DELETE).

The page MAY be unstyled or minimally styled; full polish is the responsibility of `admin-redesign` (step 4).

#### Scenario: /admin/languages page loads
- **WHEN** the client requests the languages admin page
- **THEN** the response is 200 HTML containing both a `<form>` posting to `/admin/languages` and a table seeded from `GET /admin/languages`
