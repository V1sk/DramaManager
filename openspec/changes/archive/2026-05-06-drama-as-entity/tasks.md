## 1. Schema and database layer

- [x] 1.1 Add `dramas` table DDL (`slug PK`, `name`, `default_lang`, `created_at`, `updated_at`) to `_SCHEMA` in `app/db.py`. Drop `drama_name` from the `episodes` DDL. Add `FOREIGN KEY (drama_slug) REFERENCES dramas(slug) ON DELETE RESTRICT` to the `episodes` DDL.
- [x] 1.2 Confirm `_connect()` already issues `PRAGMA foreign_keys = ON` (it does); add a unit-style test or one-off check that the FK is actually enforced (insert into episodes with unknown slug → IntegrityError).
- [x] 1.3 Replace the `ALTER TABLE` width/height back-fill loop in `init_db()` with a clean schema (no production data exists, no migration needed). Document the destructive expectation in a comment.
- [x] 1.4 Add `db.create_drama(slug, name, default_lang)` returning the new row. Validate `slug` regex, `default_lang` regex `^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$`, non-empty trimmed `name`. Raise a typed exception (e.g. `DramaExistsError`) on UNIQUE violation so the router maps it to 409.
- [x] 1.5 Add `db.get_drama(slug)` and `db.list_dramas()` (joined with `COUNT(episodes.id) AS ep_count`, ordered by `created_at DESC, slug ASC`).
- [x] 1.6 Add `db.delete_drama(slug)` returning a tuple `(deleted: bool, episode_count: int)`. Pre-check episode count; if non-zero return `(False, n)` so the router responds 409. On success delete the row; let SQLite enforce the FK as a safety net.
- [x] 1.7 Modify `db.upsert_pending(...)` to drop the `drama_name` parameter. Callers are updated accordingly.
- [x] 1.8 Modify `db.list_all()` (admin episode list) to LEFT JOIN `dramas` and select `dramas.name AS drama_name`.
- [x] 1.9 Modify `db.list_ready_dramas()` (SDK drama listing) to JOIN `dramas` so that `drama_name` comes from `dramas.name`. The `WHERE`/`GROUP BY` logic for posterUrl and lastUpdatedAt stays unchanged. Drop reliance on `MAX(e.drama_name)`.
- [x] 1.10 Modify `db.list_ready_by_slug(slug)` (per-drama episode list) — verify it doesn't read `drama_name` (it doesn't, but confirm).

## 2. Admin HTTP endpoints (drama CRUD)

- [x] 2.1 In `app/routers/admin.py` add `POST /admin/dramas` that accepts `drama_slug`, `drama_name`, `default_lang` form fields. Validate per spec; on success call `db.create_drama` and return 302 to `/admin`. Map `DramaExistsError` → 409, regex/empty failures → 400.
- [x] 2.2 Add `GET /admin/dramas` returning `[{slug, name, default_lang, ep_count, created_at, updated_at}, ...]` ordered as specified.
- [x] 2.3 Add `DELETE /admin/dramas/{slug}` (slug pattern same as elsewhere). Call `db.delete_drama`; if (`False`, `n>0`) return 409 with a message; if row not found return 404. On success `shutil.rmtree(OUT_DIR/{slug}, ignore_errors=True)` and capture warnings into the response body. Return `200 {"ok": True, "warnings": [...]}`.

## 3. Episode upload pre-check

- [x] 3.1 In `admin_upload`, after slug regex passes and before persisting/streaming the upload, call `db.get_drama(slug)`. If `None`, return HTTP 400 with a message that names the slug and points to `POST /admin/dramas`. Make sure no temp file is left on disk in that path.
- [x] 3.2 Remove the `drama_name` form parameter from the `admin_upload` signature; remove the empty-trim validation. The `drama_name` value previously persisted onto the episode row is no longer used.
- [x] 3.3 Update the `Job` dataclass / worker invocation in `app/queue.py` to drop `drama_name` (worker only logs it; logs can read from the drama row if needed, or just log the slug).

## 4. Episode delete behavior change

- [x] 4.1 In `admin_delete_episode`, remove the `if db.count_by_slug(...) == 0: shutil.rmtree(drama_dir)` block. Drama directory cleanup is now exclusively the responsibility of `DELETE /admin/dramas/{slug}`.
- [x] 4.2 The episode-dir and key-file cleanup (`shutil.rmtree(ep_dir_path)`, three key file unlinks) stays unchanged.

## 5. SDK / API readers

- [x] 5.1 Verify `app/routers/api.py` `_row_to_drama_summary` reads `drama_name` from the joined column. (No code change should be required if `db.list_ready_dramas` returns `drama_name` keyed correctly; double-check.)
- [x] 5.2 Verify `_row_to_episode_info` does not read `drama_name` (it doesn't reference it; just confirm).
- [x] 5.3 The `AdminEpisode` model and `/admin/episodes` JSON shape must continue to include `drama_name`; the value comes from the joined query.

## 6. Admin HTML template (minimal patch)

- [x] 6.1 In `templates/admin.html` add a new form above the existing upload form with fields `drama_slug`, `drama_name`, `default_lang` and a submit button posting to `/admin/dramas`. Use the same minimal styling.
- [x] 6.2 Remove the `drama_name` input from the upload form. Update its `<form>` body and the JS that reads form values.
- [x] 6.3 Show a clear inline error if a drama-create POST returns 400/409 (e.g. alert with the response body). The existing upload form already shows alerts; reuse the pattern.
- [x] 6.4 The episode list table is unchanged. Verify rendering still works because the `/admin/episodes` JSON still includes `drama_name`.

## 7. Cleanup of dead code paths

- [x] 7.1 Remove `db.count_by_slug` if no caller remains after task 4.1. (Currently only used by the episode-delete drama-dir cleanup branch we're removing.)
- [x] 7.2 Remove the back-fill `ALTER TABLE ... ADD COLUMN width/height` loop in `init_db()` (clean schema).
- [x] 7.3 Search for any remaining references to `drama_name` on `episodes`-shaped objects (`scripts/` directory, tests if any) and update.

## 8. Manual verification

- [x] 8.1 Delete `hls.db`, restart the server, confirm `init_db()` creates both tables (`PRAGMA table_info(dramas)`, `PRAGMA table_info(episodes)`).
- [x] 8.2 Try `POST /admin/upload` with no drama row → expect 400 and no episode row, no temp file remnants.
- [x] 8.3 `POST /admin/dramas` with a valid drama, then `POST /admin/upload` → episode goes through pipeline, status reaches `ready`, m3u8 plays.
- [x] 8.4 Delete the only episode of that drama → drama row stays, drama dir stays. Then `DELETE /admin/dramas/{slug}` → drama row gone, drama dir gone.
- [x] 8.5 `DELETE /admin/dramas/{slug}` with episodes still attached → 409, nothing deleted.
- [x] 8.6 `DELETE /admin/dramas/{unknown}` → 404, nothing touched.
- [x] 8.7 `GET /api/dramas` and `GET /api/episodes/{slug}/{ep}` both return shapes byte-identical to before this change for an equivalent drama+episode.

## 9. Spec sync

- [x] 9.1 After all code changes pass manual verification, run `openspec validate drama-as-entity --strict` to confirm the change artifacts are coherent.
- [x] 9.2 Update `CLAUDE.md` to describe the new drama lifecycle (drama exists → upload episodes → delete episodes → delete drama). Cross-reference `drama-entity` capability.
