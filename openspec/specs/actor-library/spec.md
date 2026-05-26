# actor-library

剧集演员库：`actors` 与 `drama_actors` 表 + 演员 CRUD / 翻译 / 关联端点 + 管理页 + SDK 演员列表端点。

## Requirements

### Requirement: actors table schema

The service SHALL persist an `actors` table with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `default_lang` (TEXT NOT NULL, FOREIGN KEY → `languages(code)` ON DELETE RESTRICT), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

The service SHALL persist a `drama_actors` junction table with columns `drama_slug` and `actor_slug`, PRIMARY KEY `(drama_slug, actor_slug)`, with FOREIGN KEY `(drama_slug) REFERENCES dramas(slug) ON DELETE CASCADE` and FOREIGN KEY `(actor_slug) REFERENCES actors(slug) ON DELETE CASCADE`.

#### Scenario: schema is created on init
- **WHEN** the service starts with an empty DB and `init_db()` runs
- **THEN** `PRAGMA table_info(actors)` lists `slug`, `default_lang`, `created_at`, `updated_at`
- **AND** `PRAGMA table_info(drama_actors)` lists `drama_slug`, `actor_slug`

#### Scenario: deleting a drama cascades drama_actors rows
- **GIVEN** `dramas` has `slug='ly'`, `actors` has `slug='zhang-san'`, `drama_actors` has `(ly, zhang-san)`
- **WHEN** the application deletes the drama
- **THEN** the row in `drama_actors` for `(ly, zhang-san)` is automatically removed

### Requirement: actor creation endpoint

The service SHALL provide `POST /admin/actors` accepting form fields `slug` (required, slug regex), `default_lang` (required, must reference an active language), and `name` (required, non-empty after trim).

On success the service SHALL atomically insert the `actors` row and a `translations` row `(entity_type='actor', entity_id=slug, lang_code=default_lang, field='name', value=name)`. Failures roll back so no partial state remains.

If the slug already exists the response is 409. If `default_lang` is missing the response is 400. If `name` is empty after trim the response is 400.

#### Scenario: valid creation succeeds
- **GIVEN** `languages` has active row `('zh-rCN', '简体中文', 1)`
- **WHEN** a client posts `slug=zhang-san`, `default_lang=zh-rCN`, `name=张三` to `POST /admin/actors`
- **THEN** the `actors` table contains the row
- **AND** the `translations` table contains `(entity_type='actor', entity_id='zhang-san', lang_code='zh-rCN', field='name', value='张三')`

#### Scenario: duplicate slug is rejected
- **GIVEN** an actor with `slug='zhang-san'` already exists
- **WHEN** a client posts the same slug
- **THEN** the response is 409

### Requirement: actor listing endpoint (admin)

The service SHALL provide `GET /admin/actors` returning a JSON array of every actor with `slug`, `default_lang`, `default_name` (translation in `default_lang`), `available_langs`, `usage_count` (number of `drama_actors` rows), `created_at`, `updated_at`. Ordering SHALL be `created_at DESC`, `slug ASC` as tie-breaker.

#### Scenario: list returns full payload per actor
- **GIVEN** an actor `zhang-san` with translations in `zh-rCN` and `en`, used by 2 dramas
- **WHEN** the client requests `GET /admin/actors`
- **THEN** the corresponding element has `available_langs=['en', 'zh-rCN']` and `usage_count=2`

### Requirement: actor default-lang update endpoint

The service SHALL provide `PATCH /admin/actors/{slug}` accepting JSON body with at most the field `default_lang`. The new value MUST reference an active language AND a translation row MUST exist for `(entity_type='actor', entity_id=slug, lang_code=new_default, field='name')`. Otherwise 400. Unknown payload fields → 400.

#### Scenario: switching default to a covered language succeeds
- **GIVEN** actor `zhang-san` with `default_lang='zh-rCN'` and translations in both `zh-rCN` and `en`
- **WHEN** the client sends `PATCH /admin/actors/zhang-san` with body `{"default_lang": "en"}`
- **THEN** the response is 200
- **AND** the row's `default_lang` is `'en'`

#### Scenario: switching default to an uncovered language is rejected
- **GIVEN** actor `zhang-san` with translation only in `zh-rCN`
- **WHEN** the client sends `PATCH /admin/actors/zhang-san` with body `{"default_lang": "ja"}`
- **THEN** the response is 400

### Requirement: actor deletion endpoint

The service SHALL provide `DELETE /admin/actors/{slug}`. If the actor exists, the service SHALL delete in this order:
1. All rows in `translations` with `entity_type='actor' AND entity_id=slug`.
2. The row in `actors` (`drama_actors` rows are cleaned by FK CASCADE).

The response is 204. Unknown slug returns 404.

#### Scenario: actor deletion cleans translations and junction
- **GIVEN** actor `zhang-san` with two translation rows and three `drama_actors` rows
- **WHEN** the client requests `DELETE /admin/actors/zhang-san`
- **THEN** the response is 204
- **AND** zero rows remain referencing `zhang-san` in `actors`, `translations`, or `drama_actors`

### Requirement: actor translation upsert endpoint

The service SHALL provide `PUT /admin/actors/{slug}/translations/{lang_code}` accepting JSON body `{"name": "..."}`. Validates `name` non-empty, actor exists, lang_code references an active language. Upserts the row `(entity_type='actor', entity_id=slug, lang_code, field='name', value=name)`. Returns 200 with the upserted row.

#### Scenario: first call inserts, second updates
- **GIVEN** actor `zhang-san` with no `en` translation
- **WHEN** the client sends `PUT /admin/actors/zhang-san/translations/en` with body `{"name": "Zhang San"}`
- **THEN** a translation row exists with `value='Zhang San'`
- **WHEN** the client sends the same request with body `{"name": "John Zhang"}`
- **THEN** the row's `value` is now `John Zhang` (not duplicated)

### Requirement: actor translation deletion endpoint

The service SHALL provide `DELETE /admin/actors/{slug}/translations/{lang_code}`. If the actor is unknown the response is 404. If `lang_code` equals the actor's `default_lang` the response is 409. Otherwise the matching translation row is deleted; the response is 204.

#### Scenario: deleting a non-default translation succeeds
- **GIVEN** actor `zhang-san` with `default_lang='zh-rCN'` and translations in both languages
- **WHEN** the client requests `DELETE /admin/actors/zhang-san/translations/en`
- **THEN** the response is 204
- **AND** only the `en` translation row is removed

#### Scenario: deleting the default-lang translation is rejected
- **GIVEN** the same actor
- **WHEN** the client requests `DELETE /admin/actors/zhang-san/translations/zh-rCN`
- **THEN** the response is 409
- **AND** the translation row is unchanged

### Requirement: drama-actor set replacement endpoint

The service SHALL provide `PUT /admin/dramas/{slug}/actors` accepting a JSON body that is an array of actor slugs (no duplicates). The drama SHALL exist (else 404). Every actor slug SHALL exist in `actors` (else 400 naming the missing slug; no rows changed).

The service SHALL atomically replace the drama's `drama_actors` rows. The response is 200 with the new actor list.

The service SHALL also provide `GET /admin/dramas/{slug}/actors` returning `[{slug, name}, ...]` localized to each actor's `default_lang`.

#### Scenario: replace from one set to another
- **GIVEN** drama `ly` is currently associated with actors `[zhang-san, li-si]`
- **WHEN** the client sends `PUT /admin/dramas/ly/actors` with body `["zhang-san", "wang-wu"]`
- **THEN** the response is 200
- **AND** `drama_actors` rows for `ly` are exactly `[(ly, zhang-san), (ly, wang-wu)]`

#### Scenario: missing actor rejects the whole request
- **GIVEN** actor `ghost` does not exist
- **WHEN** the client sends `PUT /admin/dramas/ly/actors` with body `["zhang-san", "ghost"]`
- **THEN** the response is 400
- **AND** `drama_actors` for `ly` is unchanged

### Requirement: SDK actors endpoint

The service SHALL provide `GET /api/actors` returning a JSON array of `{slug, name}` objects for every actor, where `name` is the value of the `translations` row matching `(entity_type='actor', entity_id=slug, lang_code=actor.default_lang, field='name')`. Ordering SHALL be `slug ASC`. An empty registry SHALL return `200 []`.

#### Scenario: actors listed with default-lang names
- **GIVEN** actors `zhang-san` (default zh-rCN, name `张三`) and `john-doe` (default en, name `John Doe`)
- **WHEN** the client requests `GET /api/actors`
- **THEN** the response is `[{"slug": "john-doe", "name": "John Doe"}, {"slug": "zhang-san", "name": "张三"}]`

### Requirement: minimal /admin/actors page

The service SHALL serve an HTML page (`/admin/actors` or extension) showing a create form and a table listing every actor with default-lang name, available languages, usage count, per-row delete and "manage translations" actions. Styling MAY be minimal.

#### Scenario: page loads with seeded data
- **WHEN** the client requests `/admin/actors`
- **THEN** the response is 200 HTML containing both a `<form>` posting to `/admin/actors` and a table seeded from `GET /admin/actors`
