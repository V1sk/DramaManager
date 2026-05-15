## Why

Today a "drama" exists only implicitly: it's the bag of `(drama_slug, drama_name)` strings denormalized across rows of the `episodes` table, with no place to attach drama-level metadata (synopsis, tags, actors, default language, sync status). The upcoming work — multi-language drama metadata, tags/actors libraries, manual sync to the prod business server — all assume drama is a first-class entity with its own lifecycle independent of episodes. Refactoring the schema once, before any of that work lands, lets every downstream change (i18n, tags, actors, subtitles, sync) target a stable foundation. The system has no production data, so we can rebuild the schema cleanly without migration concerns.

## What Changes

- **BREAKING** Introduce a `dramas` table keyed by `slug`. `drama_name` and `default_lang` move onto this row; `default_lang` becomes a required field at creation (BCP-47-style code, e.g. `zh-rCN`).
- **BREAKING** Drop `drama_name` from the `episodes` table. The column is gone; reads join `episodes` with `dramas` to surface the drama name.
- **BREAKING** `POST /admin/upload` no longer accepts `drama_name`. The drama must already exist when an episode is uploaded; if no row matches `drama_slug`, the request is rejected with HTTP 400.
- Add new admin endpoints for drama lifecycle: `POST /admin/dramas` (create), `GET /admin/dramas` (list), `DELETE /admin/dramas/{slug}` (delete — only when zero episodes remain).
- `GET /admin/episodes` continues to return `drama_name` per row (now sourced via join), preserving the existing admin UI's display.
- `GET /api/dramas` (SDK drama catalog) continues to expose `dramaName` per drama; sourcing changes from "MAX(drama_name) over episode rows" to "drama row".
- Episode deletion no longer auto-removes the drama. The drama row stays until explicitly deleted via `DELETE /admin/dramas/{slug}`. The on-disk drama directory is removed only when the drama row is deleted.
- Sync-status fields (`sync_status`, `sync_error`, `last_synced_at`) are deliberately **not** introduced here; they belong to the `business-server-sync` change. This change is purely an entity refactor.

## Capabilities

### New Capabilities

- `drama-entity`: dramas are first-class rows with their own lifecycle (create / list / delete). Defines the `dramas` table schema, the drama CRUD HTTP surface, and the invariant that an episode cannot exist without its drama.

### Modified Capabilities

- `hls-management-server`: the upload contract drops `drama_name` and gains a precondition that the drama exists. Persistence schema for the `episodes` table loses the `drama_name` column.
- `episode-deletion`: deleting the last episode of a drama no longer cleans up the drama directory or removes any drama-level state; cleanup is now bound to `DELETE /admin/dramas/{slug}`.
- `sdk-drama-listing`: `dramaName` in `DramaSummary` is sourced from `dramas.name` rather than aggregated from episode rows.

## Impact

- **Code**: `app/db.py` (schema + CRUD), `app/routers/admin.py` (upload no longer requires drama_name; new drama endpoints), `app/routers/api.py` (drama listing query joins `dramas`), `app/templates/admin.html` (form drops drama_name; minimal "create drama" affordance — full admin redesign comes later).
- **Schema**: `episodes` loses `drama_name`; new `dramas` table created. SQLite file is rebuilt — acceptable because no production data exists.
- **External contracts**: SDK-facing JSON shapes (`DramaSummary`, `EpisodeInfo`) are unchanged at the wire level; only the server-side sourcing changes. `episode-info-schema.json` is untouched.
- **Downstream changes** (`i18n-foundation`, `tag-library`, `actor-library`, `drama-meta-translations`, `business-server-sync`) all build on the `dramas` table introduced here.
- **No new dependencies**.
