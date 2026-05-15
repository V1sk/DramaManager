# tag-library

剧集标签库：`tags` 与 `drama_tags` 表 + 标签 CRUD / 翻译 / 关联端点 + 管理页 + SDK 标签列表端点。

## Requirements

### Requirement: tags table schema

The service SHALL persist a `tags` table with columns: `slug` (TEXT, PRIMARY KEY, matches `^[a-z0-9][a-z0-9-]*$`), `default_lang` (TEXT NOT NULL, FOREIGN KEY → `languages(code)` ON DELETE RESTRICT), `created_at` (TEXT NOT NULL, ISO 8601 UTC), `updated_at` (TEXT NOT NULL, ISO 8601 UTC).

The service SHALL persist a `drama_tags` junction table with columns `drama_slug` and `tag_slug`, PRIMARY KEY `(drama_slug, tag_slug)`, with FOREIGN KEY `(drama_slug) REFERENCES dramas(slug) ON DELETE CASCADE` and FOREIGN KEY `(tag_slug) REFERENCES tags(slug) ON DELETE CASCADE`.

#### Scenario: schema is created on init
- **WHEN** the service starts with an empty DB and `init_db()` runs
- **THEN** `PRAGMA table_info(tags)` lists `slug`, `default_lang`, `created_at`, `updated_at`
- **AND** `PRAGMA table_info(drama_tags)` lists `drama_slug`, `tag_slug`

#### Scenario: deleting a drama cascades drama_tags rows
- **GIVEN** `dramas` has `slug='ly'`, `tags` has `slug='urban'`, `drama_tags` has `(ly, urban)`
- **WHEN** the application deletes the drama
- **THEN** the row in `drama_tags` for `(ly, urban)` is automatically removed

#### Scenario: deleting a tag cascades drama_tags rows
- **GIVEN** the same setup
- **WHEN** the application deletes the tag `urban`
- **THEN** the row in `drama_tags` for `(ly, urban)` is automatically removed
- **AND** the `dramas.ly` row is unchanged

### Requirement: tag creation endpoint

The service SHALL provide `POST /admin/tags` accepting form fields `slug` (required, regex `^[a-z0-9][a-z0-9-]*$`), `default_lang` (required, must reference an active language), and `label` (required, non-empty after trim).

On success the service SHALL atomically: insert the `tags` row with `created_at = updated_at = now`; insert a row in `translations` with `(entity_type='tag', entity_id=slug, lang_code=default_lang, field='label', value=label)`. If either insert fails the operation SHALL roll back so no partial state remains.

If the slug already exists the response SHALL be 409. If `default_lang` is missing or inactive the response SHALL be 400 naming that field. If `label` is empty after trim the response SHALL be 400 naming `label`.

#### Scenario: valid creation succeeds with default-lang label
- **GIVEN** `languages` has active row `('zh-rCN', '简体中文', 1)`
- **WHEN** a client posts `slug=urban`, `default_lang=zh-rCN`, `label=都市` to `POST /admin/tags`
- **THEN** the `tags` table contains the row
- **AND** the `translations` table contains `(entity_type='tag', entity_id='urban', lang_code='zh-rCN', field='label', value='都市')`
- **AND** the response is 302 to `/admin/tags`

#### Scenario: duplicate slug is rejected
- **GIVEN** a `tags` row with `slug='urban'` already exists
- **WHEN** a client posts the same slug
- **THEN** the response is 409
- **AND** no new translation rows are inserted

### Requirement: tag listing endpoint (admin)

The service SHALL provide `GET /admin/tags` returning a JSON array of every tag joined with its default-lang label and a usage count. Each element SHALL include `slug`, `default_lang`, `default_label` (the translation in `default_lang`), `available_langs` (array of lang codes for which a translation exists), `usage_count` (number of `drama_tags` rows referencing this tag), `created_at`, `updated_at`. Ordering SHALL be `created_at DESC`, `slug ASC` as tie-breaker.

#### Scenario: list returns full payload per tag
- **GIVEN** a tag `urban` with translations in `zh-rCN` and `en`, used by 3 dramas
- **WHEN** the client requests `GET /admin/tags`
- **THEN** the corresponding element has `slug='urban'`, `available_langs=['en', 'zh-rCN']` (sorted), `usage_count=3`

### Requirement: tag default-lang update endpoint

The service SHALL provide `PATCH /admin/tags/{slug}` accepting JSON body with at most one field: `default_lang`. Other fields SHALL be rejected with 400.

The new `default_lang` MUST reference an active language AND a translation row MUST already exist for `(entity_type='tag', entity_id=slug, lang_code=new_default, field='label')`. If either fails the response is 400 naming the offending condition; the row is unchanged.

On success the row's `default_lang` is updated and `updated_at` refreshed; the response is 200 with the updated row.

#### Scenario: switching default to a covered language succeeds
- **GIVEN** tag `urban` with `default_lang='zh-rCN'` and translation rows for both `zh-rCN` and `en`
- **WHEN** the client sends `PATCH /admin/tags/urban` with body `{"default_lang": "en"}`
- **THEN** the response is 200
- **AND** the row now has `default_lang='en'`

#### Scenario: switching default to an uncovered language is rejected
- **GIVEN** tag `urban` with translation only in `zh-rCN`
- **WHEN** the client sends `PATCH /admin/tags/urban` with body `{"default_lang": "ja"}`
- **THEN** the response is 400 (no translation in `ja`)
- **AND** the row is unchanged

### Requirement: tag deletion endpoint

The service SHALL provide `DELETE /admin/tags/{slug}` (slug pattern same as elsewhere; 422 otherwise).

If the tag exists, the service SHALL delete in this order:
1. All rows in `translations` with `entity_type='tag' AND entity_id=slug`.
2. The row in `tags` (`drama_tags` rows are cleaned by FK CASCADE).

The response is 204 No Content. If the slug is unknown the response is 404.

#### Scenario: tag deletion cleans translations and junction
- **GIVEN** tag `urban` exists with translations in two languages and 3 drama_tags rows
- **WHEN** the client requests `DELETE /admin/tags/urban`
- **THEN** the response is 204
- **AND** zero rows remain in `tags`, `translations`, or `drama_tags` referencing `urban`

#### Scenario: unknown slug returns 404
- **WHEN** the client requests `DELETE /admin/tags/never-seen`
- **THEN** the response is 404

### Requirement: tag translation upsert endpoint

The service SHALL provide `PUT /admin/tags/{slug}/translations/{lang_code}` accepting JSON body `{"label": "..."}`. The label SHALL be non-empty after trim (else 400). The tag SHALL exist (else 404). The lang_code SHALL reference an active language (else 400).

The service SHALL upsert the row `(entity_type='tag', entity_id=slug, lang_code=lang_code, field='label', value=label)` — INSERT on first call, UPDATE on subsequent calls. The response is 200 with the upserted row.

#### Scenario: first call inserts, second call updates
- **GIVEN** tag `urban` exists with no `en` translation
- **WHEN** the client sends `PUT /admin/tags/urban/translations/en` with body `{"label": "Urban"}`
- **THEN** the response is 200
- **AND** a translation row exists with `value='Urban'`
- **WHEN** the client sends the same request with body `{"label": "City"}`
- **THEN** the row's `value` is now `City` (not duplicated)

### Requirement: tag translation deletion endpoint

The service SHALL provide `DELETE /admin/tags/{slug}/translations/{lang_code}`. If the tag is unknown the response is 404. If `lang_code` equals the tag's `default_lang` the response is 409 (cannot delete the default-lang label while the tag exists). Otherwise the matching translation row is deleted; the response is 204.

#### Scenario: deleting a non-default translation succeeds
- **GIVEN** tag `urban` with `default_lang='zh-rCN'` and translations in both `zh-rCN` and `en`
- **WHEN** the client requests `DELETE /admin/tags/urban/translations/en`
- **THEN** the response is 204
- **AND** only the `en` translation row is removed

#### Scenario: deleting the default-lang translation is rejected
- **GIVEN** the same tag
- **WHEN** the client requests `DELETE /admin/tags/urban/translations/zh-rCN`
- **THEN** the response is 409
- **AND** the translation row is unchanged

### Requirement: drama-tag set replacement endpoint

The service SHALL provide `PUT /admin/dramas/{slug}/tags` accepting a JSON body that is an array of tag slugs (zero or more, no duplicates). The drama SHALL exist (else 404). Every tag slug in the array SHALL exist in `tags` (else 400 naming the missing slug; no rows changed).

The service SHALL atomically replace the drama's `drama_tags` rows: delete all existing rows for the drama, insert one row per slug in the body. The response is 200 with the new tag list.

The service SHALL also provide `GET /admin/dramas/{slug}/tags` returning the drama's current tag list as `[{slug, label}, ...]` where `label` is each tag's default-lang label.

#### Scenario: replace from one set to another
- **GIVEN** drama `ly` is currently associated with tags `[urban, costume]`
- **WHEN** the client sends `PUT /admin/dramas/ly/tags` with body `["urban", "wuxia"]`
- **THEN** the response is 200
- **AND** `drama_tags` rows for `ly` are exactly `[(ly, urban), (ly, wuxia)]`

#### Scenario: missing tag rejects the whole request
- **GIVEN** drama `ly` is currently `[urban]`; tag `ghost-tag` does not exist
- **WHEN** the client sends `PUT /admin/dramas/ly/tags` with body `["urban", "ghost-tag"]`
- **THEN** the response is 400
- **AND** `drama_tags` for `ly` is unchanged (still `[urban]`)

### Requirement: SDK tags endpoint

The service SHALL provide `GET /api/tags` returning a JSON array of `{slug, label}` objects for every tag, where `label` is the value of the `translations` row matching `(entity_type='tag', entity_id=slug, lang_code=tag.default_lang, field='label')`. Ordering SHALL be `slug ASC`. An empty registry SHALL return `200 []`.

#### Scenario: tags listed with default-lang labels
- **GIVEN** tags `urban` (`default_lang='zh-rCN'`, label `都市`) and `sci-fi` (`default_lang='en'`, label `Sci-Fi`)
- **WHEN** the client requests `GET /api/tags`
- **THEN** the response is `[{"slug": "sci-fi", "label": "Sci-Fi"}, {"slug": "urban", "label": "都市"}]`

#### Scenario: empty registry returns []
- **GIVEN** the `tags` table is empty
- **WHEN** the client requests `GET /api/tags`
- **THEN** the response is 200 with body `[]`

### Requirement: minimal /admin/tags page

The service SHALL serve an HTML page (`/admin/tags` or extension to `/admin`) showing a create form (slug + default_lang select + label input) and a table listing every tag with its default-lang label, available languages, usage count, and per-row delete and "manage translations" actions. Styling MAY be minimal; full polish is deferred to `admin-redesign`.

#### Scenario: page loads with seeded data
- **WHEN** the client requests `/admin/tags`
- **THEN** the response is 200 HTML containing both a `<form>` posting to `/admin/tags` and a table seeded from `GET /admin/tags`
