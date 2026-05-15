## Context

The current SQLite schema has a single `episodes` table where the drama identity is denormalized: `drama_slug` and `drama_name` are repeated on every row. There is no row representing the drama itself. Aggregations like `GET /api/dramas` reconstruct drama-level facts via `MAX()` and `GROUP BY` over episode rows, which works for "name" and "latest update" but cannot accommodate fields with no natural per-episode aggregation: synopsis, default language, sync status, multi-language metadata, tags, actors, posters.

Six follow-up changes (`i18n-foundation`, `tag-library`, `actor-library`, `drama-meta-translations`, `episode-subtitles`, `business-server-sync`) all need a stable per-drama row. Refactoring once now — before any of those land — avoids retrofitting a foreign key six times.

The system has not been deployed and there is no production data, so destructive schema changes are acceptable. The upload pipeline (`pipeline.sh`, OSS upload, DRM key generation) is out of scope for this change and must continue to work unchanged.

## Goals / Non-Goals

**Goals:**
- A `dramas` table keyed by `slug`, owning `name` and `default_lang`.
- A foreign-key relationship from `episodes.drama_slug` to `dramas.slug`.
- HTTP surface for drama lifecycle: create, list, delete.
- `POST /admin/upload` now requires the drama to exist; the field `drama_name` is removed from the request.
- Drama deletion is the single owner of "tear down the drama directory on disk."
- Existing SDK-facing JSON shapes (`DramaSummary`, `EpisodeInfo`) stay byte-compatible; only their server-side sourcing changes.

**Non-Goals:**
- No `synopsis`, `tags`, `actors`, `poster_url`, or any multi-language fields on the drama row. Those land in subsequent changes.
- No `sync_status` / `sync_error` / `last_synced_at` columns. Those belong to `business-server-sync`.
- No FK from `dramas.default_lang` to a `languages` table — that table doesn't exist yet. The column is opaque text in this change; the FK is added in `i18n-foundation`.
- No admin UI redesign. The existing admin page gets the smallest viable edit (drop `drama_name` from the upload form; add a minimal "create drama" affordance). The full redesign is `admin-redesign` (step 4).
- No auto-increment of `ep_number`. The upload form continues to take `ep_number` explicitly. Auto-increment lands with the drama-detail page in `admin-redesign`.

## Decisions

### Decision: `dramas.slug` is the primary key (not a synthetic `id`)

The slug is already the URL component (`/videos/{slug}/...`, `/drm/{slug}/...`), the OSS path component (`Drama/{slug}/...`), the directory name on disk (`OUT_DIR/{slug}/`), and the join key in the existing schema. Promoting it to PRIMARY KEY keeps every existing reference working and avoids a useless integer indirection. The slug regex `^[a-z0-9][a-z0-9-]*$` already constrains it to URL- and filesystem-safe characters.

**Alternative considered:** synthetic `INTEGER PRIMARY KEY AUTOINCREMENT` with `slug` as a UNIQUE column. Rejected because it forces every reader (admin, SDK, OSS publisher, DRM router) to either join or carry the integer ID, gaining no semantic value.

### Decision: `episodes.drama_slug` is FK with `ON DELETE RESTRICT`

`RESTRICT` (not `CASCADE`) means deleting a drama with episodes still attached raises `IntegrityError`, surfaced as HTTP 409. This makes "delete drama only when empty" a database-level invariant rather than an application-level check. The application still pre-checks for a friendly error message, but the FK is the safety net.

**Alternative considered:** `CASCADE` would let `DELETE FROM dramas WHERE slug=?` automatically remove episodes. Rejected because episode deletion needs to clean up disk artifacts (m4s, init.mp4, OSS objects in later changes); doing this implicitly via cascade hides important side effects. Explicit "delete each episode first, then delete the drama" stays auditable.

### Decision: drop `drama_name` column from `episodes` (no soft-keep)

A naive backwards-compat plan would keep `drama_name` on `episodes` and treat the `dramas.name` row as advisory. With no production data and no SDK consumer that reads the column directly (the `episode-info-schema.json` contract has no `dramaName` field), there is no value in carrying duplicates. Reads that need the name JOIN with `dramas`. Writes update only one place.

### Decision: `default_lang` is required at drama creation, validated by permissive regex

Required because every later i18n decision (which language is the fallback? what languages must be present?) leans on it. Permissive regex `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$` accepts `zh-rCN`, `en`, `en-US`, `ja-JP`. Step 2's `i18n-foundation` introduces the canonical `languages` table; until it does, the column is opaque text. When `i18n-foundation` lands, it will add a FK constraint and migrate any out-of-vocabulary values.

**Alternative considered:** defer `default_lang` to step 2. Rejected because creating a drama without a default language means we cannot validate or render anything in step 2 without a backfill pass. Putting it in now (even if FK-less) lets later changes assume "every drama has a default_lang."

### Decision: drama directory cleanup moves out of episode deletion

Today, `DELETE /admin/episodes/{slug}/{ep}` removes `OUT_DIR/{slug}/` if `count_by_slug == 0`. With dramas as entities, the drama row outlives the last episode, so the directory must as well — the user might re-upload an episode under the same drama. Drama-directory cleanup is now bound exclusively to `DELETE /admin/dramas/{slug}`.

**Alternative considered:** leave the existing cleanup in place. Rejected because deleting "the last episode" is no longer the same event as "the drama is gone."

### Decision: HTTP layer for drama CRUD

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/dramas` | Form `drama_slug`, `drama_name`, `default_lang`. 201 on create, 409 if slug exists. |
| `GET` | `/admin/dramas` | List of `{slug, name, default_lang, ep_count, created_at, updated_at}` — admin-side. |
| `DELETE` | `/admin/dramas/{slug}` | 204 on success, 409 if any episodes remain, 404 if slug unknown. Removes `OUT_DIR/{slug}/` (whole subtree including stale `keys/`). |

`GET /admin/episodes` keeps its existing shape (each row still includes `drama_name`); the `drama_name` value comes from a join with `dramas`.

`GET /api/dramas` and `GET /api/dramas/{slug}/episodes` keep their existing shapes; their queries now LEFT JOIN `dramas` for `drama_name`. (Today they aggregate `MAX(drama_name)` over episode rows; tomorrow they read `dramas.name` directly.)

### Decision: existing admin HTML gets a minimal patch only

The current `templates/admin.html` upload form has fields `drama_slug`, `drama_name`, `ep_number`, `video`. After this change:
- The upload form drops `drama_name`. (The episode list still displays it via the joined response.)
- A small inline "create drama" form is added above the upload form: `drama_slug`, `drama_name`, `default_lang`.
- The episode-list table is unchanged.

This is intentionally ugly. The `admin-redesign` change replaces the page entirely with a drama-cards homepage + drama-detail flow.

## Risks / Trade-offs

- **Risk: `default_lang` lacks FK in step 1.** A misspelled value (`"zh-rcn"` vs `"zh-rCN"`) cannot be caught at insert time. → Permissive regex still rejects whitespace / empty / Chinese characters; `i18n-foundation` adds the FK and a one-shot reconciliation pass.
- **Risk: drama-directory cleanup ownership shift may leak files.** If a user creates a drama, uploads no episodes, then deletes the drama, the directory may not exist (no cleanup needed). If they upload then delete each episode then forget to delete the drama, the directory lingers indefinitely. → Acceptable; the directory is small (cover.jpg only after episode deletion); a future "garbage collect orphan drama dirs" cron can be added if needed.
- **Risk: the in-place admin HTML edit is jarring.** Two forms on one page, no navigation. → Accepted because the redesign is on the roadmap; degrading UX briefly is cheaper than building a stop-gap design.
- **Trade-off: no auto-increment in step 1.** Operators must still type the episode number. → Auto-increment requires the drama-detail page (which knows the slug context), so it ships with `admin-redesign`.

## Migration Plan

There is no production data, so the migration is destructive:

1. Stop the server.
2. Delete `hls.db` (and any `out/` artifacts the operator wants gone — optional, since the directory layout is unchanged).
3. Deploy the new code; `init_db()` creates both tables with the new schema on first start.
4. Re-create dramas via `POST /admin/dramas`, then re-upload episodes.

Rollback: revert the code; delete `hls.db`; the previous `init_db()` recreates the old single-table schema. Existing on-disk artifacts under `OUT_DIR/` remain valid for the old code.

## Open Questions

- Should `GET /admin/dramas` include a `default_lang` field in its response now, or wait for `i18n-foundation`? Leaning **yes, include it now** since the column exists and admin tooling can already display it; the field is just unused by SDK consumers. Will commit to this in tasks.md.
- Validation regex for `default_lang`: `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$` is permissive enough for `zh-rCN` (Android style) and `en-US` (BCP 47). Acceptable as a placeholder until `i18n-foundation`.
