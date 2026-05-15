## Why

The drama catalog needs an actor list per drama for SDK-side filtering and display, with names that are translatable per `i18n-foundation`. Actors are operator-curated and many-to-many with dramas. This change is structurally a mirror of `tag-library` (step 3a): same shape of tables, endpoints, and translation pattern. Drama-side actor projection (`DramaSummary.actors`) and `?actor=` filter are deferred to `sdk-search-and-localization` (step 6); this change owns the data and the library UI only.

## What Changes

- Introduce an `actors` table keyed by `slug` (admin-picked ASCII identifier) with a `default_lang` FK onto `languages.code`. Actor names live in `translations` under `entity_type='actor'`, `field='name'`.
- Introduce a `drama_actors` junction `(drama_slug, actor_slug)` with `ON DELETE CASCADE` on both sides.
- Add admin endpoints: actor CRUD (`POST/GET/PATCH/DELETE /admin/actors`), per-actor translation upserts (`PUT/DELETE /admin/actors/{slug}/translations/{lang_code}`), and drama–actor set replacement (`PUT /admin/dramas/{slug}/actors`).
- Add `GET /api/actors` returning every actor with the name resolved to the actor's `default_lang`. Per-request `?lang=` resolution lands in step 6.
- Add a minimal `/admin/actors` HTML page.

## Capabilities

### New Capabilities

- `actor-library`: actors table + drama_actors junction + actor CRUD + translation upserts + drama-actor-set replacement + SDK list endpoint.

### Modified Capabilities

None at this scope. SDK-side actor exposure (e.g. `actors: string[]` on `DramaSummary`, `?actor=` filter) is owned by `sdk-search-and-localization`.

## Impact

- **Code**: `app/db.py` (new tables + helpers), new router `app/routers/actors.py`, new template `templates/actors.html`, `app/main.py` mount.
- **Schema**: new `actors` table; new `drama_actors` junction; populates `translations` rows with `entity_type='actor'`.
- **External contracts**: new `GET /api/actors` endpoint. Existing endpoints unchanged.
- **No new external dependencies**.
