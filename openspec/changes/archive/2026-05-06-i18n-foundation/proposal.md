## Why

Four upcoming changes — `tag-library`, `actor-library`, `drama-meta-translations`, `episode-subtitles` — all need to attach localized strings to records (tag names, actor names, drama names, drama synopses, subtitle labels) and all need to reference the same controlled vocabulary of languages. Without a shared foundation each of those changes would re-invent its own languages table, validation rules, and CRUD UI; the four would drift apart and `dramas.default_lang` (introduced in `drama-as-entity` as opaque text) would have nothing real to point to. This change establishes the i18n substrate once: a `languages` registry, a generic `translations` store, and the FK that ties `dramas.default_lang` to a real row.

The `translations` table is built but intentionally empty — its consumers are the three per-entity changes that follow. Building it here avoids each of them having to add the same generic table.

## What Changes

- Introduce a `languages` table keyed by `code` (e.g. `zh-rCN`, `en`, `ja`), with `display_label`, `is_active` flag, and timestamps.
- Introduce a generic `translations` table keyed by `(entity_type, entity_id, lang_code, field)` storing the translated `value`. No consumer in this change populates it; it is created and FK-linked to `languages`, ready for `tag-library` / `actor-library` / `drama-meta-translations` / `episode-subtitles`.
- **BREAKING** Convert `dramas.default_lang` from an opaque-text column (added in `drama-as-entity`) to a foreign key onto `languages.code` with `ON DELETE RESTRICT`. The drama-creation precondition becomes "the chosen `default_lang` must exist as an active row in `languages`."
- Add admin endpoints for languages CRUD: `POST /admin/languages`, `GET /admin/languages`, `PATCH /admin/languages/{code}`, `DELETE /admin/languages/{code}`.
- Add a minimal `/languages` admin page (table + create form + per-row toggle/delete) so operators can populate the registry before creating dramas. Full polish is deferred to `admin-redesign` (step 4).
- Add `GET /api/languages` returning active languages — exposed for SDK so client locale pickers / subtitle UIs can enumerate available languages once translations land.
- `is_active=0` keeps a language hidden from "create drama" / "add subtitle" pickers without deleting it; references remain valid.

## Capabilities

### New Capabilities

- `i18n-foundation`: the languages registry + translations store + their CRUD surface. Defines the schema, the FK invariants (cannot delete a language referenced by `dramas.default_lang` or by any `translations` row), the active-flag semantics, and the admin/SDK endpoints.

### Modified Capabilities

- `drama-entity`: the `dramas.default_lang` column gains a foreign-key constraint onto `languages.code` (`ON DELETE RESTRICT`). The drama creation endpoint's `default_lang` validation switches from "regex" to "row exists in `languages` and is `is_active=1`."

## Impact

- **Code**: `app/db.py` (new tables, new CRUD helpers, FK on `dramas`), new router file `app/routers/languages.py` (or grow `admin.py`), `app/templates/admin.html` or a new `templates/languages.html` (minimal page), `app/main.py` (mount new router).
- **Schema**: new `languages` and `translations` tables; FK added on `dramas.default_lang`. Destructive recreate of `hls.db` is acceptable (no production data).
- **External contracts**: new `GET /api/languages` endpoint (response: array of `{code, display_label}` for active languages). No changes to `EpisodeInfo`, `DramaSummary`, or the existing SDK endpoints.
- **Downstream changes** (`tag-library`, `actor-library`, `drama-meta-translations`, `episode-subtitles`, eventually `business-server-sync`) all build on the `languages` registry and `translations` table established here.
- **No new external dependencies**.
