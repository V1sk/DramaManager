## 1. Schema and DB helpers

- [x] 1.1 Add `subtitles` table DDL to `_SCHEMA` in `app/db.py` with PK `(episode_id, lang_code)`, FK on `episodes.episode_id` CASCADE and `languages.code` RESTRICT.
- [x] 1.2 `db.upsert_subtitle(episode_id, lang_code, file_url)`: INSERT … ON CONFLICT(episode_id, lang_code) DO UPDATE; sets `uploaded_at = now`. Returns the resulting row.
- [x] 1.3 `db.list_subtitles_for_episode(episode_id)`: rows joined with `languages.display_label` as `label`. Ordered by `lang_code ASC`. Returns `[{lang_code, label, file_url, uploaded_at}, ...]`.
- [x] 1.4 `db.delete_subtitle(episode_id, lang_code)`: returns `(deleted_row | None, file_url | None)` so the route can also remove the file.
- [x] 1.5 Add `db.list_subtitles_for_slug_ep(drama_slug, ep_number)` helper that resolves `episode_id` then calls 1.3 (convenience for routes addressed by slug+ep).

## 2. Admin HTTP endpoints

- [x] 2.1 `POST /admin/episodes/{slug}/{ep}/subtitles?lang={code}` (extend `app/routers/admin.py` or new `app/routers/subtitles.py`). Validate per spec: episode exists, lang_code active, MIME ∈ {`text/vtt`, `text/plain`}, first 6 bytes start with `WEBVTT`. Strip leading UTF-8 BOM (`\xef\xbb\xbf`) before writing the rest. Write to `OUT_DIR/{slug}/ep-{n}/subtitles/{lang}.vtt`; upsert DB row.
- [x] 2.2 `GET /admin/episodes/{slug}/{ep}/subtitles`: return the list helper's output as JSON, with `file_url` renamed to `url` to match the spec response shape.
- [x] 2.3 `DELETE /admin/episodes/{slug}/{ep}/subtitles?lang={code}`: delete the row, then `unlink(missing_ok=True)` the file. Collect warnings on OSError. Return `{"ok": true, "warnings": [...]}`.
- [x] 2.4 Add path / query validation: `slug` regex, `ep` numeric, `lang` regex (BCP-47-ish like elsewhere).

## 3. Pydantic + JSON Schema

- [x] 3.1 In `app/models.py`, add a `Subtitle` model `{langCode: str, label: str, url: str}`. Add `subtitles: Optional[List[Subtitle]] = None` to `EpisodeInfo`.
- [x] 3.2 Update `episode-info-schema.json`: add an optional `subtitles` property of type `array | null`, items as objects with required `langCode`, `label`, `url`, `additionalProperties: false`. Existing payloads that omit the field continue to validate.

## 4. Internal SDK readers

- [x] 4.1 In `_row_to_episode_info` (`app/routers/api.py`), call `db.list_subtitles_for_slug_ep(slug, ep)` once. If empty list → `subtitles = None`; otherwise project to `[{langCode, label, url}, ...]` ordered by `langCode`.
- [x] 4.2 Verify the per-drama list endpoint inherits this via the shared helper (no duplicate query work).

## 5. Episode deletion interaction

- [x] 5.1 Confirm that the existing `DELETE /admin/episodes/{slug}/{ep}` flow (from `episode-deletion`) deletes subtitles transitively: FK CASCADE removes the rows, and the existing `shutil.rmtree(ep_dir_path)` removes the `subtitles/` directory. No code change required.
- [x] 5.2 Add a unit-style smoke test or manual verification step (in section 7) confirming both happen.

## 6. Admin HTML — minimal hooks

- [x] 6.1 No subtitle UI lands in this change. The endpoints exist; the upload/list UI per single-episode detail page is part of `admin-redesign` (step 4).
- [x] 6.2 (Optional, if cheap) Expose a small "subtitles" link on each episode row in the existing admin list, deep-linking to a future detail page. Skip if it adds friction; the redesign covers it.

## 7. Manual verification

- [x] 7.1 Seed languages `en` and `zh-rCN`. Create drama `ly`, upload episode 3 (status reaches `ready`).
- [x] 7.2 Prepare a valid `.vtt` file. POST it to `/admin/episodes/ly/3/subtitles?lang=en` → 200; file at `OUT_DIR/ly/ep-3/subtitles/en.vtt`; row exists.
- [x] 7.3 Try uploading an SRT file (no `WEBVTT` header) → 400.
- [x] 7.4 Upload a UTF-8-BOM-prefixed valid VTT → 200; file on disk has the BOM stripped (first 6 bytes are `WEBVTT`).
- [x] 7.5 Re-upload the same lang with a new VTT → 200; same file path now contains new bytes; `uploaded_at` is later.
- [x] 7.6 GET `/admin/episodes/ly/3/subtitles` → returns the row with `label="English"` from `languages.display_label`.
- [x] 7.7 GET `/api/episodes/ly/3` → response now includes `subtitles: [{"langCode":"en", ...}]`. Validate against the updated schema.
- [x] 7.8 GET `/api/dramas/ly/episodes` → array element for ep 3 has the same `subtitles` array.
- [x] 7.9 Add a `zh-rCN` subtitle; verify both endpoints return both, `langCode ASC`.
- [x] 7.10 DELETE the `en` subtitle → 200; row + file gone; `subtitles` array now contains only `zh-rCN`.
- [x] 7.11 DELETE the episode → cascade removes both subtitle rows; `OUT_DIR/ly/ep-3/` rmtree'd; remaining `OUT_DIR/ly/keys/ep-3.*` cleaned per `episode-deletion`.

## 8. Spec sync

- [x] 8.1 `openspec validate episode-subtitles --strict`.
- [x] 8.2 Update `episode-info-schema.json`'s `examples` array to include an example with `subtitles` populated.
