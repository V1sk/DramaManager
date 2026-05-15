## MODIFIED Requirements

### Requirement: drama poster upload endpoint

The service SHALL provide `POST /admin/dramas/{slug}/poster?lang={lang_code}` accepting a multipart upload with a `file` part. Acceptable content types: `image/jpeg`, `image/png`, `image/webp`. Other types SHALL respond 400.

The drama SHALL exist (else 404). The lang_code SHALL reference an active language (else 400). The drama SHALL already have a `name` translation in this lang_code (else 400 — poster cannot exist for a language with no name).

The handler SHALL:
1. Determine the new file extension from MIME.
2. Remove any existing poster file at `OUT_DIR/{slug}/poster/{lang_code}.*` (any extension).
3. Write the upload to `OUT_DIR/{slug}/poster/{lang_code}.{new_ext}`.
4. **When `settings.oss_enabled`**: call `publish.unpublish_poster_from_staging(slug, lang_code)` to clear any prior-extension OSS object, then call `publish.upload_poster_to_staging(slug, lang_code, local_path)` to mirror the new file to OSS staging.
5. Upsert `translations` with `field='poster'`, `value='/videos/{slug}/poster/{lang_code}.{new_ext}'`.

If step 3 fails the handler SHALL roll back step 2 (best-effort) and respond 500.

If step 4's OSS upload fails the handler SHALL unlink the local file written in step 3 (rolling back to the prior state) and respond 500. The translation row MUST NOT be updated. The DB and OSS state remain consistent post-rollback.

The response on success is 200 with the new poster URL (the relative `/videos/...` URL — the OSS URL is an internal mirror, not exposed by this endpoint).

#### Scenario: first poster upload writes file and translation
- **GIVEN** drama `ly` with `name` translation in `en`, no poster yet, OSS mode enabled
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** `OUT_DIR/ly/poster/en.jpg` exists
- **AND** OSS object `Drama/staging/ly/poster/en.jpg` exists with the same bytes
- **AND** `translations` has `(drama, ly, en, poster, '/videos/ly/poster/en.jpg')`
- **AND** the response is 200 with that URL

#### Scenario: poster upload with OSS disabled stays local-only
- **GIVEN** OSS mode disabled, drama `ly` with `name` translation in `en`
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** `OUT_DIR/ly/poster/en.jpg` exists
- **AND** no OSS upload is attempted
- **AND** the translation row is upserted normally

#### Scenario: poster upload rolls back on OSS failure
- **GIVEN** OSS mode enabled, drama `ly`, the OSS service is temporarily unreachable
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** the response is 500
- **AND** `OUT_DIR/ly/poster/en.jpg` does NOT exist (rolled back)
- **AND** no `(drama, ly, en, poster)` translation row was created or updated

#### Scenario: poster upload replaces a different-extension predecessor
- **GIVEN** OSS mode enabled, drama `ly` has `OUT_DIR/ly/poster/en.png` and `Drama/staging/ly/poster/en.png` already
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** local `en.png` is removed
- **AND** local `en.jpg` is written
- **AND** OSS `Drama/staging/ly/poster/en.png` is removed
- **AND** OSS `Drama/staging/ly/poster/en.jpg` is created
- **AND** the translation row's value points to `.jpg`

#### Scenario: poster upload rejected without name translation
- **GIVEN** drama `ly` has no `name` translation in `ja`
- **WHEN** the client posts an `image/jpeg` to `POST /admin/dramas/ly/poster?lang=ja`
- **THEN** the response is 400
- **AND** no file is written
- **AND** no OSS upload is attempted

#### Scenario: poster upload rejects unknown content type
- **WHEN** the client posts `application/pdf` to `POST /admin/dramas/ly/poster?lang=en`
- **THEN** the response is 400
- **AND** no file is written
- **AND** no OSS upload is attempted

### Requirement: drama poster deletion endpoint

The service SHALL provide `DELETE /admin/dramas/{slug}/poster?lang={lang_code}`. If the drama is unknown the response is 404. If no poster translation exists for `(slug, lang_code)` the response is 404 (with a different message: poster not found).

Otherwise the service SHALL:
1. Delete the translation row.
2. Delete the on-disk file (any extension).
3. **When `settings.oss_enabled`**: call `publish.unpublish_poster_from_staging(slug, lang_code)` to clear staging OSS objects under `Drama/staging/{slug}/poster/{lang_code}.*`. OSS failures MUST NOT roll back the DB / disk delete; they SHALL be logged at WARNING level.

The response is 204 No Content.

This endpoint SHALL be allowed even when `lang_code = default_lang` — the drama's default-language **name** is required, but the **poster** is optional and may be removed independently.

Prod-side OSS cleanup is NOT triggered by this endpoint; deferred to the manual sync flow which uses prefix-level `unpublish_drama_from_prod` on full-drama delete-sync.

#### Scenario: delete poster also clears staging OSS
- **GIVEN** OSS mode enabled, drama `ly` with poster translation + local file + staging OSS object for `zh-rCN`
- **WHEN** the client requests `DELETE /admin/dramas/ly/poster?lang=zh-rCN`
- **THEN** the response is 204
- **AND** the poster translation row is gone
- **AND** the local file is gone
- **AND** OSS objects under `Drama/staging/ly/poster/zh-rCN.*` are gone
- **AND** any prod OSS object remains unchanged (cleanup deferred to sync)

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
- **AND** no OSS call is attempted

#### Scenario: poster delete tolerates OSS failure
- **GIVEN** OSS mode enabled, drama `ly` with poster in `zh-rCN`, OSS unreachable
- **WHEN** the client requests `DELETE /admin/dramas/ly/poster?lang=zh-rCN`
- **THEN** the response is still 204 (DB + local file successfully removed)
- **AND** the OSS failure is logged at WARNING level
