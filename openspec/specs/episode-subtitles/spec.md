# episode-subtitles

剧集字幕能力：`subtitles` 表 + WebVTT 文件存储约定 + 上传 / 列表 / 删除端点 + `EpisodeInfo.subtitles` 字段。

## Requirements

### Requirement: subtitles table schema

The service SHALL persist a `subtitles` table with columns: `episode_id` (TEXT NOT NULL, FOREIGN KEY → `episodes(episode_id)` ON DELETE CASCADE), `lang_code` (TEXT NOT NULL, FOREIGN KEY → `languages(code)` ON DELETE RESTRICT), `file_url` (TEXT NOT NULL, host-relative), `uploaded_at` (TEXT NOT NULL, ISO 8601 UTC). PRIMARY KEY `(episode_id, lang_code)`.

#### Scenario: schema is created on init
- **WHEN** the service starts with an empty DB and `init_db()` runs
- **THEN** `PRAGMA table_info(subtitles)` lists `episode_id`, `lang_code`, `file_url`, `uploaded_at`

#### Scenario: deleting an episode cascades subtitle rows
- **GIVEN** `episodes` has `episode_id='ly-ep-3'` and `subtitles` has rows for that episode in two languages
- **WHEN** the application deletes the episode
- **THEN** zero rows remain in `subtitles` for `episode_id='ly-ep-3'`

#### Scenario: deleting a language referenced by subtitles is rejected at the DB layer
- **GIVEN** `languages` has `code='en'` and `subtitles` has at least one row with `lang_code='en'`
- **WHEN** the application attempts `DELETE FROM languages WHERE code='en'`
- **THEN** SQLite raises `IntegrityError`

### Requirement: subtitle file storage convention

Subtitle files SHALL be stored at `OUT_DIR/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt` where `ep_dir = "ep-{ep_number}"`. The corresponding `subtitles.file_url` SHALL be `/videos/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt`.

The `/videos/` static mount SHALL serve these files transparently (no router-level handling needed beyond the existing static mount at `OUT_DIR`).

#### Scenario: uploaded subtitle ends up at the expected path
- **GIVEN** an episode `(slug='ly', ep_number=3)` exists with `episode_id='ly-ep-3'`
- **WHEN** the operator uploads a WebVTT file for `lang=en`
- **THEN** the file `OUT_DIR/ly/ep-3/subtitles/en.vtt` exists
- **AND** the row `subtitles(episode_id='ly-ep-3', lang_code='en', file_url='/videos/ly/ep-3/subtitles/en.vtt', uploaded_at=now)` exists

### Requirement: subtitle upload endpoint

The service SHALL provide `POST /admin/episodes/{drama_slug}/{ep}/subtitles?lang={lang_code}` accepting a multipart upload with a single `file` part. The route parameters SHALL match the patterns used elsewhere (`drama_slug` matches `^[a-z0-9][a-z0-9-]*$`; `ep` matches `^[0-9]+$`); query parameter `lang` is required.

The handler SHALL validate, in order:
1. The episode `(drama_slug, ep)` exists with any status (else 404; subtitles can be uploaded before the episode reaches `ready`).
2. The `lang_code` references an active language (else 400).
3. The upload's MIME type is `text/vtt` or `text/plain` (else 400).
4. The first 6 bytes of the upload start with `WEBVTT` (else 400).

On success the service SHALL atomically:
1. Ensure the directory `OUT_DIR/{drama_slug}/{ep_dir}/subtitles/` exists.
2. Write the upload to `OUT_DIR/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt` (overwriting any prior). Strip a leading UTF-8 BOM if present, before writing the rest of the bytes.
3. Upsert the `subtitles` row with `file_url='/videos/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt'` and `uploaded_at = now`.

The response is 200 with the new row's contents.

#### Scenario: first valid VTT upload writes file and row
- **GIVEN** episode `ly-ep-3` exists, language `en` is active
- **WHEN** the operator posts a `text/vtt` file (starts with `WEBVTT\n\n00:00:00.000 -->...`)
- **THEN** the file `OUT_DIR/ly/ep-3/subtitles/en.vtt` exists
- **AND** the response is 200 with `{"episode_id":"ly-ep-3","lang_code":"en","file_url":"/videos/ly/ep-3/subtitles/en.vtt","uploaded_at":...}`

#### Scenario: re-upload overwrites the file and bumps uploaded_at
- **GIVEN** the prior subtitle exists at `OUT_DIR/ly/ep-3/subtitles/en.vtt` with bytes `B1`
- **WHEN** the operator uploads a new file with bytes `B2` for the same `(episode, lang)`
- **THEN** the file at the same path now contains `B2`
- **AND** the row's `uploaded_at` is the new timestamp
- **AND** no duplicate row is created

#### Scenario: non-VTT MIME is rejected
- **WHEN** the operator posts an `application/octet-stream` file
- **THEN** the response is 400
- **AND** no file is written

#### Scenario: file without WEBVTT magic is rejected
- **WHEN** the operator posts a file whose first 6 bytes are `1\n00:0` (looks like SRT)
- **THEN** the response is 400 with a message indicating the file does not start with `WEBVTT`
- **AND** no file is written

#### Scenario: missing episode returns 404
- **WHEN** the operator posts a subtitle for a non-existent `(slug, ep)`
- **THEN** the response is 404

#### Scenario: inactive language is rejected
- **GIVEN** language `ja` exists with `is_active=0`
- **WHEN** the operator posts a subtitle for `lang=ja`
- **THEN** the response is 400

### Requirement: subtitle listing endpoint

The service SHALL provide `GET /admin/episodes/{drama_slug}/{ep}/subtitles` returning a JSON array `[{lang_code, label, url, uploaded_at}, ...]` ordered by `lang_code ASC`. `label` is the corresponding `languages.display_label`.

If the episode is unknown the response is 404.

#### Scenario: listing returns labels from languages.display_label
- **GIVEN** episode `ly-ep-3` has subtitle rows for `en` and `zh-rCN`; `languages` has `('en','English',1)` and `('zh-rCN','简体中文',1)`
- **WHEN** the client requests `GET /admin/episodes/ly/3/subtitles`
- **THEN** the response is `[{"lang_code":"en","label":"English","url":"/videos/ly/ep-3/subtitles/en.vtt","uploaded_at":"..."},{"lang_code":"zh-rCN","label":"简体中文","url":"/videos/ly/ep-3/subtitles/zh-rCN.vtt","uploaded_at":"..."}]`

#### Scenario: episode with no subtitles returns []
- **GIVEN** episode `ly-ep-3` exists but has no subtitle rows
- **WHEN** the client requests `GET /admin/episodes/ly/3/subtitles`
- **THEN** the response is 200 with body `[]`

### Requirement: subtitle deletion endpoint

The service SHALL provide `DELETE /admin/episodes/{drama_slug}/{ep}/subtitles?lang={lang_code}`. If the episode is unknown the response is 404. If no subtitle row matches `(episode_id, lang_code)` the response is 404.

Otherwise the service SHALL delete the on-disk file (any path equal to the row's `file_url` mapped back to disk) and the DB row, in that order. File-not-found during delete is tolerated; other OS errors are logged and returned as `warnings` in the response body.

The success response is `200 {"ok": true, "warnings": [...]}`.

#### Scenario: deleting an existing subtitle removes file and row
- **GIVEN** subtitle exists for `(ly-ep-3, en)`, file at `OUT_DIR/ly/ep-3/subtitles/en.vtt`
- **WHEN** the client requests `DELETE /admin/episodes/ly/3/subtitles?lang=en`
- **THEN** the response is 200 with empty `warnings`
- **AND** the row is gone
- **AND** the file is gone

#### Scenario: deleting a missing subtitle returns 404
- **GIVEN** episode `ly-ep-3` has no `ja` subtitle
- **WHEN** the client requests `DELETE /admin/episodes/ly/3/subtitles?lang=ja`
- **THEN** the response is 404

### Requirement: EpisodeInfo subtitles field

The Pydantic model `EpisodeInfo` and the JSON schema `episode-info-schema.json` SHALL declare an optional, nullable `subtitles` field of type `array | null`. Each array item SHALL be `{langCode: string, label: string, url: string}` with `additionalProperties: false`.

When at least one subtitle row exists for the episode, the API SHALL return the array sorted by `langCode ASC`. When no subtitle rows exist, the API SHALL return `null` (not `[]`), matching the convention used for `coverUrl` / `drm` / `fallback`.

#### Scenario: episode with subtitles renders the array
- **GIVEN** ready episode `ly-ep-3` with subtitles for `en` and `zh-rCN`
- **WHEN** the client requests `GET /api/episodes/ly/3`
- **THEN** the response includes `"subtitles": [{"langCode":"en","label":"English","url":"/videos/ly/ep-3/subtitles/en.vtt"},{"langCode":"zh-rCN","label":"简体中文","url":"/videos/ly/ep-3/subtitles/zh-rCN.vtt"}]`
- **AND** the response validates against the updated `episode-info-schema.json`

#### Scenario: episode without subtitles renders null
- **GIVEN** ready episode `ly-ep-3` with no subtitle rows
- **WHEN** the client requests `GET /api/episodes/ly/3`
- **THEN** the response includes `"subtitles": null`
- **AND** the response validates against `episode-info-schema.json`

#### Scenario: subtitles field appears in per-drama list endpoint
- **GIVEN** ready episode `ly-ep-3` with one subtitle in `en`
- **WHEN** the client requests `GET /api/dramas/ly/episodes`
- **THEN** the array element for `ep-3` includes the same `subtitles` array as the single-episode endpoint, byte-identical
