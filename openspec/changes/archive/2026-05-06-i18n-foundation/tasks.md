## 1. Schema and database layer

- [x] 1.1 Add `languages` table DDL (`code TEXT PRIMARY KEY`, `display_label TEXT NOT NULL`, `is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))`, `created_at`, `updated_at`) to `_SCHEMA` in `app/db.py`.
- [x] 1.2 Add `translations` table DDL (`entity_type`, `entity_id`, `lang_code`, `field`, `value`, `PRIMARY KEY(entity_type, entity_id, lang_code, field)`, `FOREIGN KEY(lang_code) REFERENCES languages(code) ON DELETE RESTRICT`).
- [x] 1.3 Modify the `dramas` DDL to add `FOREIGN KEY (default_lang) REFERENCES languages(code) ON DELETE RESTRICT`. The `default_lang` regex check (from `drama-as-entity`) is removed from the create helper; existence + active validation in the create helper replaces it.
- [x] 1.4 Confirm `_connect()` issues `PRAGMA foreign_keys = ON` (it does); add a one-off self-test on startup that inserts a translation with a missing lang_code and expects `IntegrityError`, just to verify FK enforcement isn't silently dropped.

## 2. DB helpers — languages CRUD

- [x] 2.1 `db.create_language(code, display_label)`: validate code regex, non-empty trimmed label; insert with `is_active=1`. Raise `LanguageExistsError` on PK collision (mapped to 409 by the router).
- [x] 2.2 `db.list_languages(active_only: bool = False)`: returns rows ordered by `code ASC`. When `active_only=True`, filters `is_active=1` (used by `GET /api/languages`).
- [x] 2.3 `db.get_language(code)`: single row or `None`.
- [x] 2.4 `db.update_language(code, *, display_label=None, is_active=None)`: only updates the fields explicitly passed; refresh `updated_at`. Returns the updated row or `None` if not found. Validate `display_label` non-empty, `is_active` ∈ {0, 1, True, False}.
- [x] 2.5 `db.delete_language(code)`: pre-check references — count drama rows with `default_lang=code` and translation rows with `lang_code=code`. If any, return `(False, {"dramas": n_d, "translations": n_t})`. Else delete and return `(True, {...})`. Let SQLite enforce the FK as a safety net.
- [x] 2.6 Update `db.create_drama(...)` (added in `drama-as-entity`): replace the regex check on `default_lang` with an existence-and-active check via `db.get_language(default_lang)`. Raise a typed exception `LanguageNotFoundError` or `LanguageInactiveError` so the router maps to 400.

## 3. Admin HTTP endpoints — languages CRUD

- [x] 3.1 Create `app/routers/languages.py` (or extend `admin.py`) with `POST /admin/languages` accepting form fields. On success → `db.create_language` → 302 to `/admin/languages`. Map exceptions: invalid → 400, exists → 409.
- [x] 3.2 Add `GET /admin/languages` returning JSON of all rows (ordering as specified). All rows, regardless of `is_active`.
- [x] 3.3 Add `PATCH /admin/languages/{code}` accepting JSON body. Reject unknown fields (including `code` itself) with 400. 404 if row missing. On success return 200 with the updated row.
- [x] 3.4 Add `DELETE /admin/languages/{code}`. 404 if missing. 409 with `{"error": "language is referenced", "dramas": n, "translations": m}` if references exist. 204 on success.
- [x] 3.5 Mount the new router in `app/main.py`.

## 4. SDK endpoint

- [x] 4.1 In `app/routers/api.py`, add `GET /api/languages` returning `[{"code": ..., "display_label": ...}, ...]` for `is_active=1` rows only. Order by `code ASC`. Empty registry → `200 []`.

## 5. Admin HTML template

- [x] 5.1 Create `app/templates/languages.html` (or expand `admin.html`) with: a create form (POST `/admin/languages`), a table listing all languages from `GET /admin/languages` with toggle/delete buttons. Minimal styling; reuse existing CSS variables.
- [x] 5.2 Add a navigation link from `/admin` to `/admin/languages` (a small `<a>` near the page header). The full nav bar is `admin-redesign`'s job.
- [x] 5.3 Wire the toggle button to `PATCH /admin/languages/{code}` with body `{"is_active": <new>}`, and the delete button to `DELETE /admin/languages/{code}` with confirm dialog. On 409 from delete, surface the `dramas`/`translations` counts in an alert.

## 6. Drama creation flow update

- [x] 6.1 The existing "create drama" form in `admin.html` (added in `drama-as-entity`) currently has a free-text `default_lang` input. Change it to a `<select>` populated by `GET /admin/languages` (active rows only). The first active language is preselected; if no active languages exist, the form disables the submit and shows a hint to "create a language first."
- [x] 6.2 The `POST /admin/dramas` endpoint already validates `default_lang`; with the helper update in 2.6 the message points to "create the language first" when missing.

## 7. Cleanup

- [x] 7.1 Drop the `default_lang` regex helper / constant in `db.py` if it's no longer referenced after 2.6 (FK is now the source of truth; helper exists only for the routing layer's quick reject of garbage that wouldn't pass FK either — keep if it gives a friendlier error than `IntegrityError`, otherwise remove).
- [x] 7.2 Update `CLAUDE.md` to document the languages registry and how to seed it before creating dramas. Cross-reference `i18n-foundation` capability.

## 8. Manual verification

- [x] 8.1 Delete `hls.db`, start server. Confirm `init_db()` creates `languages` and `translations` tables (`PRAGMA table_info`).
- [x] 8.2 `POST /admin/dramas` without any language in the registry → 400 naming `default_lang`. Drama is not created.
- [x] 8.3 `POST /admin/languages` with `code=zh-rCN, display_label=简体中文` → 201/302. `GET /admin/languages` shows it. `GET /api/languages` returns it.
- [x] 8.4 `POST /admin/dramas` with `default_lang=zh-rCN` → succeeds. The drama row has FK pointing to the language.
- [x] 8.5 `PATCH /admin/languages/zh-rCN` with `{"is_active": false}` → 200. `GET /api/languages` no longer includes it. `GET /admin/languages` still includes it (with `is_active=0`).
- [x] 8.6 Try to create a new drama with `default_lang=zh-rCN` while it's inactive → 400.
- [x] 8.7 Try `DELETE /admin/languages/zh-rCN` while a drama references it → 409 with `dramas: 1`. Delete the drama (incl. all episodes) first, then retry → 204.
- [x] 8.8 Insert a fake `translations` row via DB (`INSERT INTO translations VALUES ('drama','test','en','name','test')`) referencing a different language; try to delete that language → 409 with `translations: 1`.

## 9. Spec sync

- [x] 9.1 Run `openspec validate i18n-foundation --strict`.
- [x] 9.2 If `drama-as-entity` is archived before this change is implemented, re-validate to ensure the MODIFIED reference to `drama-entity` resolves cleanly.
