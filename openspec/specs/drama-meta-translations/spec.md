# drama-meta-translations

剧 (drama) 元信息多语言化：drama 的 `name` / `synopsis` / `poster` 改由 `translations` 表存储，新增 drama 翻译 / 海报上传 / 列表 / 删除等管理端点。

## Requirements

### Requirement: drama translations storage

The service SHALL store every drama's translatable fields in the `translations` table with `entity_type='drama'`, `entity_id=<dramas.slug>`, `lang_code=<existing language code>`, `field` ∈ `{'name', 'synopsis', 'poster'}`. The `dramas` table SHALL NOT carry `name`, `synopsis`, or `poster_url` columns. Drama identity is the `(slug, default_lang)` pair plus translation rows.

#### Scenario: dramas schema does not include name / synopsis / poster columns
- **WHEN** `init_db()` runs
- **THEN** `PRAGMA table_info(dramas)` lists exactly `slug`, `default_lang`, `created_at`, `updated_at`
- **AND** does NOT include `name`, `synopsis`, or `poster_url`

#### Scenario: drama name is sourced from translations on read
- **GIVEN** `dramas` has `('ly', 'zh-rCN')` and `translations` has `('drama', 'ly', 'zh-rCN', 'name', '琅琊榜')`
- **WHEN** any reader (e.g. admin list, SDK list) needs the drama's name
- **THEN** the value `'琅琊榜'` comes from the `translations` row, joined on default_lang

### Requirement: drama poster file storage convention

Drama poster files SHALL be stored at `OUT_DIR/{slug}/poster/{lang_code}.{ext}` where `{ext}` is determined by the uploaded MIME type. Accepted MIME types: `image/jpeg` → `jpg`, `image/png` → `png`, `image/webp` → `webp`. The corresponding URL persisted in `translations.value` SHALL be `/videos/{slug}/poster/{lang_code}.{ext}` (host-relative, served by the existing `/videos/` static mount).

When a poster is re-uploaded with a different MIME type, the prior file with the old extension SHALL be removed before the new file is written, so each `(slug, lang_code)` has at most one poster file on disk at any time.

#### Scenario: uploading a JPEG poster writes the expected path
- **GIVEN** a drama `slug='ly'` and a language `lang='zh-rCN'`
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=zh-rCN`
- **THEN** the file `OUT_DIR/ly/poster/zh-rCN.jpg` exists
- **AND** the translation row `('drama', 'ly', 'zh-rCN', 'poster', '/videos/ly/poster/zh-rCN.jpg')` exists

#### Scenario: switching MIME types removes the old file
- **GIVEN** `OUT_DIR/ly/poster/zh-rCN.jpg` exists
- **WHEN** the client uploads an `image/png` for the same drama+lang
- **THEN** `OUT_DIR/ly/poster/zh-rCN.jpg` no longer exists
- **AND** `OUT_DIR/ly/poster/zh-rCN.png` exists
- **AND** the translation row's `value` is updated to `/videos/ly/poster/zh-rCN.png`

### Requirement: drama-create endpoint persists name as a translation

The service's existing `POST /admin/dramas` (form: `drama_slug`, `drama_name`, `default_lang`) SHALL atomically:
1. Insert the `dramas` row with `created_at = updated_at = now`.
2. Insert the translation row `(entity_type='drama', entity_id=slug, lang_code=default_lang, field='name', value=drama_name)`.

Both inserts SHALL share a transaction; either succeeds or both roll back. Validation rules (slug regex, drama_name non-empty after trim, default_lang exists) are the same as before; only the persistence target changes.

#### Scenario: drama creation writes name to translations
- **GIVEN** `languages` has active `('zh-rCN', '简体中文', 1)`
- **WHEN** a client posts `drama_slug=ly`, `drama_name=琅琊榜`, `default_lang=zh-rCN`
- **THEN** `dramas` has a row `('ly', 'zh-rCN', ...)` (no `name` column)
- **AND** `translations` has the row `('drama', 'ly', 'zh-rCN', 'name', '琅琊榜')`

#### Scenario: failed translation insert rolls back drama row
- **GIVEN** the application encounters a DB error inserting the translation row (e.g. simulated)
- **WHEN** the client posts a drama
- **THEN** the `dramas` row does not exist after the request returns
- **AND** the translation row does not exist

### Requirement: drama translation upsert endpoint

The service SHALL provide `PUT /admin/dramas/{slug}/translations/{lang_code}` accepting JSON body with optional `name` and optional `synopsis` fields. At least one of the two SHALL be present (else 400). Both, when present, SHALL be non-empty after trim (else 400).

The drama SHALL exist (else 404). The lang_code SHALL reference an active language (else 400). If the drama has no existing `name` translation in this `lang_code`, the request body MUST include `name` (else 400 — cannot set synopsis-only on a fresh language).

For each present field, the service SHALL upsert the row `(entity_type='drama', entity_id=slug, lang_code, field=<field>, value=<value>)`. Absent fields SHALL NOT be touched.

The response is 200 with the resulting per-language content `{lang_code, name?, synopsis?, poster?}`.

#### Scenario: upsert name and synopsis together
- **GIVEN** drama `ly` exists with `default_lang='zh-rCN'` and `name` already in `zh-rCN`
- **WHEN** the client sends `PUT /admin/dramas/ly/translations/en` with body `{"name": "Langya Bang", "synopsis": "A wuxia revenge tale."}`
- **THEN** the response is 200
- **AND** two translation rows exist for `(drama, ly, en, name)` and `(drama, ly, en, synopsis)`

#### Scenario: synopsis-only update on existing language
- **GIVEN** drama `ly` already has `name` and `synopsis` translations in `en`
- **WHEN** the client sends `PUT /admin/dramas/ly/translations/en` with body `{"synopsis": "Updated synopsis."}`
- **THEN** the response is 200
- **AND** the `synopsis` row's `value` is updated
- **AND** the `name` row is unchanged

#### Scenario: synopsis-only on a fresh language is rejected
- **GIVEN** drama `ly` has no translations in `ja`
- **WHEN** the client sends `PUT /admin/dramas/ly/translations/ja` with body `{"synopsis": "..."}`
- **THEN** the response is 400 (name is required for fresh languages)
- **AND** no translation rows exist for `(drama, ly, ja, *)`

### Requirement: drama translation deletion endpoint

The service SHALL provide `DELETE /admin/dramas/{slug}/translations/{lang_code}`. If the drama is unknown the response is 404. If `lang_code` equals the drama's `default_lang` the response is 409. Otherwise the service SHALL atomically:
1. Delete every `translations` row matching `(entity_type='drama', entity_id=slug, lang_code=lang_code)`.
2. Delete the on-disk poster file `OUT_DIR/{slug}/poster/{lang_code}.{ext}` (any extension). File-not-found is tolerated; other OSError → log warning + include in response `warnings`.

The response is `200 {"ok": true, "warnings": [...]}` on success.

#### Scenario: deleting a non-default translation removes name, synopsis, and poster
- **GIVEN** drama `ly` (default `zh-rCN`) has translations in `en` for name + synopsis + poster, with file `OUT_DIR/ly/poster/en.jpg`
- **WHEN** the client requests `DELETE /admin/dramas/ly/translations/en`
- **THEN** the response is 200 with empty `warnings`
- **AND** zero `translations` rows remain for `(drama, ly, en, *)`
- **AND** `OUT_DIR/ly/poster/en.jpg` no longer exists

#### Scenario: deleting the default-lang translation is rejected
- **GIVEN** the same drama
- **WHEN** the client requests `DELETE /admin/dramas/ly/translations/zh-rCN`
- **THEN** the response is 409
- **AND** no translation rows are removed
- **AND** the poster file is unchanged

### Requirement: drama poster upload endpoint

The service SHALL provide `POST /admin/dramas/{slug}/poster?lang={lang_code}` accepting a multipart upload with a `file` part. Acceptable content types: `image/jpeg`, `image/png`, `image/webp`. Other types SHALL respond 400.

The drama SHALL exist (else 404). The lang_code SHALL reference an active language (else 400). The drama SHALL already have a `name` translation in this lang_code (else 400 — poster cannot exist for a language with no name).

The handler SHALL:
1. Determine the new file extension from MIME.
2. Remove any existing poster file at `OUT_DIR/{slug}/poster/{lang_code}.*` (any extension).
3. Write the upload to `OUT_DIR/{slug}/poster/{lang_code}.{new_ext}`.
4. Upsert `translations` with `field='poster'`, `value='/videos/{slug}/poster/{lang_code}.{new_ext}'`.

If step 3 fails the handler SHALL roll back step 2 (best-effort) and respond 500. The response on success is 200 with the new poster URL.

#### Scenario: first poster upload writes file and translation
- **GIVEN** drama `ly` with `name` translation in `en`, no poster yet
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** `OUT_DIR/ly/poster/en.jpg` exists
- **AND** `translations` has `(drama, ly, en, poster, '/videos/ly/poster/en.jpg')`
- **AND** the response is 200 with that URL

#### Scenario: poster upload rejected without name translation
- **GIVEN** drama `ly` has no `name` translation in `ja`
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=ja`
- **THEN** the response is 400
- **AND** no file is written

#### Scenario: poster upload rejects unknown content type
- **WHEN** the client posts `application/pdf` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** the response is 400
- **AND** no file is written

### Requirement: drama poster deletion endpoint

The service SHALL provide `DELETE /admin/dramas/{slug}/poster?lang={lang_code}`. If the drama is unknown the response is 404. If no poster translation exists for `(slug, lang_code)` the response is 404 (with a different message: poster not found).

Otherwise the service SHALL delete the translation row + the on-disk file (any extension). The response is 204 No Content.

This endpoint SHALL be allowed even when `lang_code = default_lang` — the drama's default-language **name** is required, but the **poster** is optional and may be removed independently.

#### Scenario: delete poster for default lang is allowed
- **GIVEN** drama `ly` (default `zh-rCN`) has a poster translation and file for `zh-rCN`
- **WHEN** the client requests `DELETE /admin/dramas/ly/poster?lang=zh-rCN`
- **THEN** the response is 204
- **AND** the poster row is gone
- **AND** the file is gone
- **AND** the `name` translation for `zh-rCN` is unchanged

#### Scenario: deleting a missing poster returns 404
- **GIVEN** drama `ly` with no poster translation in `en`
- **WHEN** the client requests `DELETE /admin/dramas/ly/poster?lang=en`
- **THEN** the response is 404

### Requirement: drama translations listing endpoint

The service SHALL provide `GET /admin/dramas/{slug}/translations` returning a JSON object grouped by lang_code:

```
{
  "zh-rCN": {"name": "...", "synopsis": "...", "poster": "/videos/.../zh-rCN.jpg"},
  "en":     {"name": "...", "synopsis": null,  "poster": null}
}
```

Every lang_code that has at least one translation row for this drama SHALL be a key. Within each language's object, missing fields SHALL be `null` (not absent). Ordering of keys is by `lang_code ASC`.

If the drama is unknown the response is 404.

#### Scenario: returns per-lang nested content
- **GIVEN** drama `ly` with translations in `zh-rCN` (name + synopsis + poster) and `en` (name only)
- **WHEN** the client requests `GET /admin/dramas/ly/translations`
- **THEN** the response is 200 with
  ```
  {
    "en":     {"name": "Langya Bang", "synopsis": null, "poster": null},
    "zh-rCN": {"name": "琅琊榜", "synopsis": "...", "poster": "/videos/ly/poster/zh-rCN.jpg"}
  }
  ```
