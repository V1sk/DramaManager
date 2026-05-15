## 1. Schema migration

- [x] 1.1 Update the `dramas` DDL in `app/db.py` to drop the `name` column. The new schema is `(slug PK, default_lang FK, created_at, updated_at)`.
- [x] 1.2 Document in code that the destructive recreate is acceptable (no production data) — same posture as `drama-as-entity` and `i18n-foundation`.

## 2. DB helpers — drama translations

- [x] 2.1 Modify `db.create_drama(slug, name, default_lang)`: in a transaction, INSERT into `dramas` (no name column), then INSERT translation `(drama, slug, default_lang, name, name_value)`. Roll back on any failure.
- [x] 2.2 `db.update_drama_default_lang(slug, new_default_lang)`: pre-check that `(drama, slug, new_default_lang, name)` translation exists; refresh `updated_at`. Typed exceptions for missing / inactive language and missing translation.
- [x] 2.3 `db.upsert_drama_translation(slug, lang_code, *, name=None, synopsis=None)`: writes/updates rows for present fields only. Enforces "name required for fresh language" rule.
- [x] 2.4 `db.delete_drama_translation(slug, lang_code)`: 409 if `lang_code = default_lang`; otherwise delete all `translations` rows for `(drama, slug, lang_code)`. Does NOT touch poster file (file deletion is in the route handler so warnings can be collected).
- [x] 2.5 `db.list_drama_translations(slug)`: returns the nested `{lang_code: {name?, synopsis?, poster?}}` shape from the spec.
- [x] 2.6 `db.upsert_drama_poster(slug, lang_code, url)` and `db.delete_drama_poster(slug, lang_code)`: upsert/delete the `(drama, slug, lang_code, poster, url)` translation row.
- [x] 2.7 `db.delete_drama(slug)` (existing): also `DELETE FROM translations WHERE entity_type='drama' AND entity_id=slug` before deleting the dramas row. The `OUT_DIR/{slug}/` rmtree handles `poster/` transitively.

## 3. Internal SDK readers

- [x] 3.1 Update `db.list_ready_dramas` to JOIN `translations` for `(drama, slug, default_lang, name)` and project as `drama_name`. Replace any prior reliance on `dramas.name`.
- [x] 3.2 Update `db.list_all` (admin episode list) similarly to surface `drama_name` via translations join.
- [x] 3.3 Update `db.list_dramas` (admin drama list, from `drama-as-entity`) similarly.
- [x] 3.4 Verify `_row_to_drama_summary` in `app/routers/api.py` reads the joined `drama_name`.
- [x] 3.5 Add a defensive empty-string fallback when the default-lang `name` translation is unexpectedly missing (defense in depth — POST `/admin/dramas` should prevent this, but a missing-name shouldn't break list responses).

## 4. Admin HTTP endpoints

- [x] 4.1 Add `PATCH /admin/dramas/{slug}` accepting JSON `{"default_lang"}` only. Map exceptions: 404 / 400.
- [x] 4.2 Add `GET /admin/dramas/{slug}/translations` returning the nested per-lang shape.
- [x] 4.3 Add `PUT /admin/dramas/{slug}/translations/{lang_code}` accepting `{"name"?, "synopsis"?}`. Validate per spec; upsert; return 200 with the resulting per-lang content.
- [x] 4.4 Add `DELETE /admin/dramas/{slug}/translations/{lang_code}` with default-lang guard. Atomically: delete translations + delete poster file (warnings collected). Return `{"ok": true, "warnings": [...]}`.
- [x] 4.5 Add `POST /admin/dramas/{slug}/poster?lang={code}` multipart handler. Validate MIME, drama existence, lang_code active, name translation exists. Sequence: remove old file → write new file → upsert translation. Return new URL.
- [x] 4.6 Add `DELETE /admin/dramas/{slug}/poster?lang={code}`: remove translation row + on-disk file (any extension). Return 204 on success, 404 if translation missing.

## 5. Static-mount poster path

- [x] 5.1 Verify the existing `/videos` static mount at `app/main.py` covers `OUT_DIR/{slug}/poster/{lang_code}.{ext}` paths transparently (it should — the mount root is `OUT_DIR`). No code change expected; just confirm by manual GET.

## 6. Admin HTML — minimal hooks

- [x] 6.1 Update `templates/admin.html` "create drama" form: no shape change (still `drama_slug`, `drama_name`, `default_lang`); just confirm it still works after handler rewires name to translations.
- [x] 6.2 No drama-detail page in this change. The endpoints exist; UI to drive them lands in `admin-redesign` (step 4). Optionally add a tiny "manage translations" link per drama in the drama list.

## 7. Manual verification

- [x] 7.1 Delete `hls.db`, restart. Confirm `dramas` schema has no `name` column.
- [x] 7.2 Seed `zh-rCN`. POST `/admin/dramas` → row exists; translations row exists; `GET /api/dramas` (after uploading at least one ready episode) returns the drama with the right `dramaName`.
- [x] 7.3 PUT `/admin/dramas/ly/translations/en` with `{"name": "Langya Bang", "synopsis": "..."}` → translations rows present; `GET /admin/dramas/ly/translations` returns nested shape.
- [x] 7.4 PATCH `/admin/dramas/ly` with `{"default_lang": "en"}` → succeeds; `GET /api/dramas` now returns `Langya Bang`.
- [x] 7.5 Attempt PATCH to a lang with no `name` → 400.
- [x] 7.6 POST poster (jpeg) for `en` → file at `OUT_DIR/ly/poster/en.jpg`, URL persisted; `GET /videos/ly/poster/en.jpg` serves the file.
- [x] 7.7 Re-upload poster as png → old jpg gone, new png file present, translation URL updated.
- [x] 7.8 DELETE poster for `zh-rCN` (default lang) → succeeds (default-lang poster is removable, default-lang name is not).
- [x] 7.9 DELETE the drama → 200; all translations gone; `OUT_DIR/ly/` gone (including `poster/`).

## 8. Spec sync

- [x] 8.1 `openspec validate drama-meta-translations --strict`.
