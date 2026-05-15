## 1. Schema and DB helpers

- [x] 1.1 Add `actors` and `drama_actors` DDL to `_SCHEMA` in `app/db.py` with FKs as specified.
- [x] 1.2 `db.create_actor(slug, default_lang, name)`: validate inputs; transactional insert into `actors` + `translations`. Typed exceptions.
- [x] 1.3 `db.list_actors()`: rows joined with default-lang name + `available_langs` + `usage_count`.
- [x] 1.4 `db.get_actor(slug)` and `db.delete_actor(slug)` (delete cleans translations rows then the row; cascades handle drama_actors).
- [x] 1.5 `db.update_actor_default_lang(slug, new_default_lang)`: pre-check that a translation exists; refresh `updated_at`.
- [x] 1.6 `db.upsert_actor_translation(slug, lang_code, name)` and `db.delete_actor_translation(slug, lang_code)` (default-lang guard).
- [x] 1.7 `db.replace_drama_actors(drama_slug, actor_slugs)` and `db.list_drama_actors(drama_slug)`.
- [x] 1.8 Consider extracting a generic `_upsert_named_translation(entity_type, entity_id, lang_code, field, value)` helper shared with `tag-library` if this change is implemented after step 3a. If 3a is implemented first, refactor at that point.

## 2. Admin HTTP endpoints

- [x] 2.1 New router `app/routers/actors.py` mirroring the `tag-library` router structure. All endpoints from the design's HTTP layer table.
- [x] 2.2 Map typed exceptions to HTTP status codes consistently with `tag-library`.
- [x] 2.3 Mount router in `app/main.py`.

## 3. SDK endpoint

- [x] 3.1 Add `GET /api/actors` to `app/routers/api.py`. Return `[{slug, name}]` ordered by slug.

## 4. Admin HTML

- [x] 4.1 Create `templates/actors.html` with create form + listing + per-row actions. Reuse styles.
- [x] 4.2 Add navigation link.
- [x] 4.3 "Manage translations" inline panel with PUT + DELETE wired.

## 5. Manual verification

- [x] 5.1 Seed languages.
- [x] 5.2 Create actor `zhang-san` with default `zh-rCN` and name `张三`. `GET /api/actors` returns `[{"slug": "zhang-san", "name": "张三"}]`.
- [x] 5.3 Add an English translation; switch default to `en`; SDK now returns `Zhang San`.
- [x] 5.4 Try to delete the default-lang translation → 409.
- [x] 5.5 Create a drama; PUT `["zhang-san"]` to its actors; verify `GET /admin/dramas/{slug}/actors`.
- [x] 5.6 Delete the drama; verify `drama_actors` row gone, actor remains.
- [x] 5.7 Delete the actor; verify cascade across `drama_actors` and `translations`.

## 6. Spec sync

- [x] 6.1 `openspec validate actor-library --strict`.
