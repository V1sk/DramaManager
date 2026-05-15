## Why

The drama catalog needs filterable categories (`urban`, `sci-fi`, etc.) that are translatable per the user-locale rules established in `i18n-foundation`. Tags are operator-curated and many-to-many with dramas. This change introduces the tag library and its drama association table, builds on the `translations` table to carry localized labels, and exposes both an admin CRUD surface and a basic SDK list endpoint. Drama-side filtering by tag is deferred to `sdk-search-and-localization` (step 6); this change owns the data and the library UI only.

## What Changes

- Introduce a `tags` table keyed by `slug` (admin-picked ASCII identifier, regex `^[a-z0-9][a-z0-9-]*$`) with a `default_lang` FK onto `languages.code`. Tag labels live in the `translations` table under `entity_type='tag'`, `field='label'`.
- Introduce a `drama_tags` junction table `(drama_slug, tag_slug)` with `ON DELETE CASCADE` on both sides, so deleting a drama or a tag automatically cleans up associations.
- Add admin endpoints for tag CRUD (`POST/GET/DELETE /admin/tags`), per-tag translation management (`PUT/DELETE /admin/tags/{slug}/translations/{lang_code}`), and drama–tag set management (`PUT /admin/dramas/{slug}/tags`).
- Add `GET /api/tags` returning every tag with its label localized to the tag's `default_lang`. Per-request `?lang=` resolution lands in step 6.
- Add a minimal `/admin/tags` HTML page (table + create form). Full polish comes with `admin-redesign`.

## Capabilities

### New Capabilities

- `tag-library`: tags table + drama_tags junction + tag CRUD + translation upserts + drama-tag-set replacement + SDK list endpoint.

### Modified Capabilities

None at this scope. Drama-side tag exposure (e.g. `tags: string[]` on `DramaSummary`, `?tag=` filter) is owned by `sdk-search-and-localization` (step 6).

## Impact

- **Code**: `app/db.py` (new tables, helpers), new router `app/routers/tags.py`, new template `templates/tags.html` (or `admin.html` extension), `app/main.py` (mount).
- **Schema**: new `tags` table; new `drama_tags` junction; populates `translations` rows with `entity_type='tag'`.
- **External contracts**: new `GET /api/tags` endpoint. Existing endpoints unchanged.
- **No new external dependencies**.
