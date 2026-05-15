## Why

The drama row today (post `drama-as-entity` + `i18n-foundation`) carries `name` as a single-language column. The user requirements call for drama name, synopsis, and poster to all be multi-language — operators upload Chinese / English / Japanese variants, and the SDK eventually picks per locale. This change moves the drama name out of the row and into the `translations` table, introduces synopsis as a translation-only field, and introduces multi-language drama posters as files with their URLs stored in the translations table. The drama row keeps only its identity (`slug`) and `default_lang`. SDK locale negotiation (`?lang=` resolution, drama-summary projection of synopsis/posters) is owned by `sdk-search-and-localization` (step 6); this change preserves byte-compatible SDK output by serving the default-lang translation everywhere the old `dramas.name` column was read.

## What Changes

- **BREAKING** Drop the `name` column from the `dramas` table. Drama identity is now `(slug, default_lang)` plus translations.
- **BREAKING** `POST /admin/dramas` continues to accept `drama_name` as a form field, but the value is now persisted as a `translations` row (`entity_type='drama'`, `entity_id=slug`, `lang_code=default_lang`, `field='name'`) rather than as a column. The form shape is unchanged for callers.
- Add `synopsis` as a translatable field — same pattern as `name` (field='synopsis'). No row-level column. Optional at drama creation; settable per-language afterwards.
- Add multi-language drama posters: file storage under `OUT_DIR/{slug}/poster/{lang_code}.{ext}`; URL `/videos/{slug}/poster/{lang_code}.{ext}`; URL value persisted in `translations` (`field='poster'`).
- New endpoints: `PUT /admin/dramas/{slug}/translations/{lang_code}` (upsert `name` and/or `synopsis` together), `DELETE /admin/dramas/{slug}/translations/{lang_code}` (gated by default-lang guard), `POST /admin/dramas/{slug}/poster?lang={code}` (multipart file upload), `DELETE /admin/dramas/{slug}/poster?lang={code}`, `GET /admin/dramas/{slug}/translations` (returns full per-lang content).
- Update internal SDK readers (`GET /api/dramas`, `GET /api/dramas/{slug}/episodes`, `GET /admin/episodes`, `GET /admin/dramas`): the `dramaName` value is now resolved via `translations` joined on `(entity_type='drama', entity_id=slug, lang_code=dramas.default_lang, field='name')`. Wire shape is byte-identical.
- Allow changing a drama's `default_lang` via `PATCH /admin/dramas/{slug}` — guarded by the same "translation exists for the new default" rule used in `tag-library` and `actor-library`.

## Capabilities

### New Capabilities

- `drama-meta-translations`: drama-name + synopsis + poster, all multi-language, stored in the generic `translations` table with `entity_type='drama'`. CRUD endpoints, file storage convention for posters.

### Modified Capabilities

- `drama-entity`: the `dramas` table loses the `name` column; drama creation persists the name through translations; new `PATCH /admin/dramas/{slug}` for `default_lang` changes.
- `sdk-drama-listing`: `dramaName` is now resolved through `translations` (default-lang fallback). Wire-format unchanged; data sourcing changes.

## Impact

- **Code**: `app/db.py` (drop `dramas.name` column; add translation upsert helpers; rewire drama list queries), `app/routers/admin.py` (drama-create handler routes name to translations; new PATCH endpoint), `app/routers/api.py` (drama listing readers), new poster-upload handler (could go in `admin.py` or a dedicated `dramas.py`), `templates/admin.html` minimal UI for translation management on the (still minimal) drama detail.
- **Schema**: `dramas.name` removed; `translations` accumulates rows with `entity_type='drama'`; new disk convention `OUT_DIR/{slug}/poster/{lang_code}.{ext}`.
- **External contracts**: byte-compatible. `episode-info-schema.json` is untouched. No new fields on `DramaSummary` or `EpisodeInfo` in this change. (Step 6 may add `synopsis` / per-locale `poster` to `DramaSummary`.)
- **No new external dependencies**.
