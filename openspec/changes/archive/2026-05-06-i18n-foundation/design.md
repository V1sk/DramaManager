## Context

After `drama-as-entity` lands, `dramas.default_lang` is required text validated only by a permissive regex — no actual language registry exists yet. Subsequent changes (`tag-library`, `actor-library`, `drama-meta-translations`, `episode-subtitles`) all need:
1. A controlled vocabulary of languages that operators can grow over time.
2. A place to store translated strings keyed by language.
3. A guarantee that no orphan `lang_code` values can sneak in.

Building this once avoids each downstream change duplicating the registry, the validation, and the admin UI for managing languages. The system has no production data, so adding a foreign key onto `dramas.default_lang` is a clean re-create rather than a migration.

## Goals / Non-Goals

**Goals:**
- A `languages` table keyed by an i18n code (`zh-rCN`, `en`, `ja-JP`, ...).
- A generic `translations` table that any entity can write into, keyed by `(entity_type, entity_id, lang_code, field)`.
- FK from `dramas.default_lang` → `languages.code` enforced at the DB layer.
- Admin CRUD HTTP surface for the languages library.
- One SDK endpoint (`GET /api/languages`) listing active languages, ready for downstream features that surface language pickers.
- A minimal admin page so operators can populate the registry before creating dramas.

**Non-Goals:**
- No translation rows are written or read in this change. The `translations` table is purely structural; `tag-library`, `actor-library`, `drama-meta-translations`, and `episode-subtitles` populate it.
- No locale resolution / fallback logic on the API side. That belongs to `sdk-search-and-localization` (step 5 in the roadmap).
- No `is_default` (system-wide) flag on languages. Per-drama defaulting (`dramas.default_lang`) is sufficient; an additional global default would only encode the per-drama policy twice.
- No locale-aware admin UI; admin chrome stays in Chinese.
- No SDK contract changes for `EpisodeInfo` / `DramaSummary`. Those happen in `sdk-search-and-localization`.

## Decisions

### Decision: `languages.code` is the primary key

The code itself is the natural identifier. Every translation row, every drama default, every future business-server payload references this string. A synthetic integer `id` would force a join everywhere and provide no semantic value. The code regex is identical to the one validated in `drama-as-entity`: `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`.

### Decision: generic `translations` table over per-entity translation tables

A single `translations(entity_type, entity_id, lang_code, field, value)` table replaces what would otherwise be `drama_translations`, `tag_translations`, `actor_translations`, etc. The composite primary key `(entity_type, entity_id, lang_code, field)` makes upsert trivial and prevents duplicate entries.

**Trade-off:** the `entity_id` column is `TEXT` (not a typed FK), so the table cannot reference per-entity tables with a real FK. Orphan rows (e.g. drama-id pointing at a deleted drama) are possible. → Mitigations: each consumer change is responsible for cleaning up its own translation rows on entity delete (single `DELETE FROM translations WHERE entity_type=? AND entity_id=?`), and a future audit script can detect orphans.

**Alternatives considered:**
- Per-entity translation tables (`drama_translations`, `tag_translations`, ...): cleaner FKs, but multiplies tables and migration steps. Rejected for ergonomic reasons.
- JSON column on each entity: simple but un-queryable ("which dramas have a Japanese synopsis?" requires a JSON scan). Rejected because step 5 (`sdk-search-and-localization`) needs to filter and project by `lang_code`.

### Decision: `lang_code` FK is `ON DELETE RESTRICT` (not CASCADE)

Deleting a language with referencing translations or drama defaults must fail loudly. A silent CASCADE could destroy thousands of translation rows and break drama defaults invisibly. The admin must explicitly clean up references first, then delete the language; or use `is_active=0` to hide it from new entries while preserving existing references.

### Decision: `is_active` flag separate from existence

Operators may want to retire a language without deleting it (e.g. legal team disables a market). Hard delete is gated by FK references; soft hide via `is_active=0` is always available.

The flag's semantics:
- **Admin endpoints** (`GET /admin/languages`, listing) return all rows regardless of `is_active`. Admins need to see hidden ones.
- **SDK endpoint** (`GET /api/languages`) returns only `is_active=1` rows.
- **Drama creation** validation requires the chosen `default_lang` to be both present **and** `is_active=1`.

### Decision: re-create `hls.db` rather than ALTER TABLE for the new FK

Adding a FK to an existing column in SQLite requires a table rebuild (`CREATE TABLE … AS SELECT … ; DROP; RENAME`). With no production data, the simpler path is: drop both `dramas` and `episodes` (FK chain), recreate them with the new schema. The startup `init_db()` does the right thing on a fresh DB; the operator deletes `hls.db` once at deploy time.

This matches the migration posture taken in `drama-as-entity`. Operationally it's "stop the server, delete the DB file, redeploy." Documented in the migration plan.

### Decision: HTTP layer

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/languages` | Form `code`, `display_label`. Defaults `is_active=1`. 201 / 302 on create, 409 if code exists. |
| `GET` | `/admin/languages` | Array of `{code, display_label, is_active, created_at, updated_at}`. All rows. |
| `PATCH` | `/admin/languages/{code}` | JSON body with optional `display_label` and/or `is_active`. 404 if unknown. |
| `DELETE` | `/admin/languages/{code}` | 204 on success. 409 if any drama or translation references it. 404 if unknown. |
| `GET` | `/api/languages` | Array of `{code, display_label}` for `is_active=1` rows only. Ordered by `code ASC`. |

`PATCH` (rather than `PUT`) because partial updates are useful (toggle `is_active` without re-sending the label). Form-encoded for `POST` to fit the existing admin form posture; JSON for `PATCH` because there is no HTML form for it (only the admin JS).

### Decision: minimal admin UI in this change

A small `/languages` page is added — a table of all languages plus an inline create form. No styling beyond what the existing `admin.html` provides. The full admin redesign (`admin-redesign`, step 4) replaces this with a polished library page; the minimal page exists only so that downstream changes (3a/3b/3c/3d) can populate languages before they need them.

**Alternative considered:** API-only in step 2, no UI. Rejected because requiring `curl` to seed the registry would block local testing of the next four changes.

## Risks / Trade-offs

- **Risk: `translations` table is created but unused for several changes.** It might accumulate test data and stale schema if downstream changes pivot. → The table schema is dirt simple (5 columns); it's nearly free to leave empty. If consumers pivot away from it, dropping it is a one-line migration.
- **Risk: `is_active=0` doesn't propagate to `dramas.default_lang`.** A drama created when `en` was active will keep `default_lang='en'` even after operators set `en.is_active=0`. → That's the intended behavior — soft-hide affects only future creations / lists. If ops want to rip `en` out entirely, they delete the language, which RESTRICTS until drama defaults are reassigned.
- **Risk: `entity_id` in `translations` is TEXT with no FK enforcement.** Orphan rows are possible if a consumer change forgets to clean up. → Each consumer change carries the cleanup responsibility in its tasks.md; an audit script can be added later.
- **Trade-off: no SDK locale resolution yet.** `GET /api/languages` exposes the list, but the SDK can't yet say "give me the Japanese version of this drama" — that lands in `sdk-search-and-localization`. Acceptable: SDK has nothing to localize yet anyway (no translations exist).

## Migration Plan

1. Stop the server.
2. Delete `hls.db`.
3. Deploy the new code; `init_db()` creates the new schema (`dramas`, `episodes`, `languages`, `translations`) on first start.
4. Operator opens `/admin/languages`, creates at least one language (e.g. `zh-rCN` / 简体中文).
5. Operator can now create dramas (POST `/admin/dramas` with a `default_lang` that exists in `languages`).
6. Episode upload / pipeline / OSS / DRM flows are untouched.

Rollback: revert code, delete `hls.db`, re-run prior schema.

## Open Questions

- Should `PATCH /admin/languages/{code}` reject changes to `code` itself (i.e. is the primary key immutable)? Leaning **yes, immutable**: changing a language code would orphan every reference. Rename = delete + recreate, gated by FK references. Will be in tasks.md.
- Should `GET /api/languages` cache-control? Probably `Cache-Control: public, max-age=300` since languages change rarely; but this is a tiny optimization deferred to `sdk-search-and-localization` when SDK actually starts hitting it.
