## MODIFIED Requirements

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
3. **When `settings.oss_enabled`**: call `publish.upload_subtitle_to_staging(drama_slug, ep_dir, lang_code, local_path)`. On OSS failure unlink the just-written local file and respond 500.
4. Upsert the `subtitles` row with `file_url='/videos/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt'` and `uploaded_at = now`.

The response is 200 with the new row's contents.

#### Scenario: first valid VTT upload writes file, OSS object, and row
- **GIVEN** episode `ly-ep-3` exists, language `en` is active, OSS mode enabled
- **WHEN** the operator posts a `text/vtt` file (starts with `WEBVTT\n\n00:00:00.000 -->...`)
- **THEN** the file `OUT_DIR/ly/ep-3/subtitles/en.vtt` exists
- **AND** OSS object `Drama/staging/ly/ep-3/subtitles/en.vtt` exists with the same bytes (BOM stripped)
- **AND** the response is 200 with `{"episode_id":"ly-ep-3","lang_code":"en","file_url":"/videos/ly/ep-3/subtitles/en.vtt","uploaded_at":...}`

#### Scenario: subtitle upload with OSS disabled stays local-only
- **GIVEN** OSS mode disabled, episode `ly-ep-3` exists, language `en` is active
- **WHEN** the operator posts a valid VTT file
- **THEN** `OUT_DIR/ly/ep-3/subtitles/en.vtt` exists
- **AND** no OSS upload is attempted
- **AND** the row is upserted normally

#### Scenario: re-upload overwrites the file and OSS object
- **GIVEN** the prior subtitle exists at `OUT_DIR/ly/ep-3/subtitles/en.vtt` with bytes `B1` AND `Drama/staging/ly/ep-3/subtitles/en.vtt` also has `B1`
- **WHEN** the operator uploads a new file with bytes `B2` for the same `(episode, lang)`
- **THEN** the file at the same path now contains `B2`
- **AND** the OSS object now contains `B2`
- **AND** the row's `uploaded_at` is the new timestamp
- **AND** no duplicate row is created

#### Scenario: subtitle upload rolls back on OSS failure
- **GIVEN** OSS mode enabled, OSS service unreachable, episode `ly-ep-3`, language `en`
- **WHEN** the operator posts a valid VTT file
- **THEN** the response is 500
- **AND** `OUT_DIR/ly/ep-3/subtitles/en.vtt` does NOT exist (rolled back)
- **AND** no subtitle row was upserted

#### Scenario: non-VTT MIME is rejected
- **WHEN** the operator posts an `application/octet-stream` file
- **THEN** the response is 400
- **AND** no file is written
- **AND** no OSS upload is attempted

#### Scenario: file without WEBVTT magic is rejected
- **WHEN** the operator posts a file whose first 6 bytes are `1\n00:0` (looks like SRT)
- **THEN** the response is 400 with a message indicating the file does not start with `WEBVTT`
- **AND** no file is written
- **AND** no OSS upload is attempted

#### Scenario: missing episode returns 404
- **WHEN** the operator posts a subtitle for a non-existent `(slug, ep)`
- **THEN** the response is 404

#### Scenario: inactive language is rejected
- **GIVEN** language `ja` exists with `is_active=0`
- **WHEN** the operator posts a subtitle for `lang=ja`
- **THEN** the response is 400

### Requirement: subtitle deletion endpoint

The service SHALL provide `DELETE /admin/episodes/{drama_slug}/{ep}/subtitles?lang={lang_code}`. If the episode is unknown the response is 404. If no subtitle row matches `(episode_id, lang_code)` the response is 404.

Otherwise the service SHALL:
1. Delete the on-disk file (any path equal to the row's `file_url` mapped back to disk).
2. Delete the DB row.
3. **When `settings.oss_enabled`**: call `publish.unpublish_subtitle_from_staging(drama_slug, ep_dir, lang_code)`. OSS failures MUST NOT roll back DB / disk; logged at WARNING and surfaced via `warnings` array.

File-not-found during local delete is tolerated; other OS errors are logged and returned as `warnings`. The success response is `200 {"ok": true, "warnings": [...]}`.

Prod-side OSS cleanup is NOT triggered here; deferred to the manual sync flow (which uses `unpublish_episode_from_prod` on episode delete-sync).

#### Scenario: deleting an existing subtitle clears file, row, and staging OSS
- **GIVEN** OSS mode enabled, subtitle exists for `(ly-ep-3, en)` locally and at `Drama/staging/ly/ep-3/subtitles/en.vtt`
- **WHEN** the client requests `DELETE /admin/episodes/ly/3/subtitles?lang=en`
- **THEN** the response is 200 with empty `warnings`
- **AND** the row is gone
- **AND** the local file is gone
- **AND** the OSS object `Drama/staging/ly/ep-3/subtitles/en.vtt` is gone
- **AND** any prod OSS object remains unchanged (cleanup deferred to sync)

#### Scenario: deleting a missing subtitle returns 404
- **GIVEN** episode `ly-ep-3` has no `ja` subtitle
- **WHEN** the client requests `DELETE /admin/episodes/ly/3/subtitles?lang=ja`
- **THEN** the response is 404
- **AND** no OSS call is attempted

#### Scenario: subtitle delete tolerates OSS failure
- **GIVEN** OSS mode enabled, subtitle row exists, OSS unreachable
- **WHEN** the client requests `DELETE /admin/episodes/ly/3/subtitles?lang=en`
- **THEN** the response is 200 (DB + local removed)
- **AND** the OSS failure is logged at WARNING level
