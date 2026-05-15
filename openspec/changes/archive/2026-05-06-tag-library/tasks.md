## 1. Schema and DB helpers

- [x] 1.1 Add `tags` and `drama_tags` DDL to `_SCHEMA` in `app/db.py` with FKs as specified.
- [x] 1.2 `db.create_tag(slug, default_lang, label)`: validate inputs; in a single transaction insert into `tags` + `translations`. Raise `TagExistsError` on PK collision; `LanguageNotFoundError` / `LanguageInactiveError` mirror i18n-foundation's typed exceptions.
- [x] 1.3 `db.list_tags()`: returns rows joined with default-lang label + array of available langs + usage_count via subqueries or LEFT JOINs.
- [x] 1.4 `db.get_tag(slug)` and `db.delete_tag(slug)` (delete cleans translations rows first, then the row; cascades handle drama_tags).
- [x] 1.5 `db.update_tag_default_lang(slug, new_default_lang)`: pre-check that a translation exists for the new lang; refresh `updated_at`.
- [x] 1.6 `db.upsert_tag_translation(slug, lang_code, label)` and `db.delete_tag_translation(slug, lang_code)` (with default-lang guard returning a typed exception).
- [x] 1.7 `db.replace_drama_tags(drama_slug, tag_slugs)`: in a transaction, validate every slug exists, delete existing junction rows, insert new ones.
- [x] 1.8 `db.list_drama_tags(drama_slug)`: returns `[{slug, label}]` joined with default-lang labels.

## 2. Admin HTTP endpoints

- [x] 2.1 New router `app/routers/tags.py` with all endpoints from the design's HTTP layer table.
- [x] 2.2 Map typed exceptions to HTTP status codes consistently (409 / 400 / 404 / 422).
- [x] 2.3 Mount router in `app/main.py`.

## 3. SDK endpoint

- [x] 3.1 Add `GET /api/tags` to `app/routers/api.py`. Reuse `db.list_tags` (or a slimmer projection) and return `[{slug, label}]` ordered by slug.

## 4. Admin HTML

- [x] 4.1 Create `templates/tags.html` with the create form + listing table + per-row actions. Reuse styles from `admin.html`.
- [x] 4.2 Add a navigation link to `/admin/tags` in the existing admin chrome.
- [x] 4.3 The "manage translations" action opens a small inline panel listing all language translations for the tag, with PUT + DELETE actions wired.

## 5. Manual verification

- [x] 5.1 Seed at least two languages via `/admin/languages`.
- [x] 5.2 Create a tag `urban` with `default_lang=zh-rCN`, label `都市`. `GET /api/tags` shows `[{"slug": "urban", "label": "都市"}]`.
- [x] 5.3 PUT an English translation: `urban` now exposes `available_langs=['en', 'zh-rCN']` in admin; SDK still shows the default-lang label.
- [x] 5.4 Switch default to `en` via PATCH; `GET /api/tags` now shows `Urban`.
- [x] 5.5 Try to delete the default-lang translation → 409.
- [x] 5.6 Create a drama; PUT `["urban"]` to its tags; `GET /admin/dramas/{slug}/tags` shows the tag.
- [x] 5.7 Delete the drama; verify `drama_tags` row is gone but the tag itself remains.
- [x] 5.8 Delete the tag; verify all translations + any remaining junction rows are gone.

## 6. Spec sync

- [x] 6.1 `openspec validate tag-library --strict`.
