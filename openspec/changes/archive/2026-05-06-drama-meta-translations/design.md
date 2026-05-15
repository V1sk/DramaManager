## Context

After `drama-as-entity` and `i18n-foundation`, the drama row has `slug`, `name`, `default_lang`, timestamps. Tags and actors use `(slug, default_lang)` + translations for their name. Drama metadata should follow the same pattern; in addition, synopsis and poster (a file URL) are also multi-language. Once this change lands, every translatable entity in the system follows the identical shape: row carries identity + default_lang; translations carry per-language fields.

The system has no production data, so dropping the `dramas.name` column is a clean recreate.

## Goals / Non-Goals

**Goals:**
- Move drama name into the `translations` table.
- Add synopsis (text) and poster (file URL) as translation fields.
- Convention for poster files: one image per language under `OUT_DIR/{slug}/poster/{lang_code}.{ext}`, URL relative to `/videos/`.
- Per-language CRUD (text) and per-language poster upload/delete endpoints.
- Internal callers that previously read `dramas.name` continue to work, sourcing from translations under default-lang.
- A `PATCH /admin/dramas/{slug}` endpoint to retarget `default_lang` when the operator wants to switch the drama's primary language.

**Non-Goals:**
- No SDK contract additions (`synopsis`, locale-aware `posterUrl`, etc.). Step 6 owns `sdk-search-and-localization`.
- No `?lang=` query resolution on existing endpoints. Step 6.
- No drama summary view that aggregates name + synopsis + poster + tags + actors in one shot. Step 4 (`admin-redesign`) builds that page.

## Decisions

### Decision: drop `dramas.name` instead of keeping it as a denormalized cache

Two sources of truth (column + translation row) would inevitably drift. The column adds zero query complexity — every reader joins anyway because the default-lang translation is the source. With no production data, dropping is safe.

### Decision: poster URL goes in `translations.value` as a relative URL

A separate `drama_posters` table would duplicate the (entity_id, lang_code) key already encoded in translations. Storing the URL in `translations.value` with `field='poster'` keeps the schema flat. The file lives on disk; the URL is the pointer.

The URL is **relative** (`/videos/{slug}/poster/{lang_code}.jpg`) consistent with all other locally-served URLs (`playUrl`, `coverUrl`, `keyUri`). When OSS staging/prod separation lands (step 5), poster files will move under the staging prefix and the URL will be rewritten there.

### Decision: poster file storage `OUT_DIR/{slug}/poster/{lang_code}.{ext}`

Each language gets one poster file, keyed by lang_code. `{ext}` is determined from the upload's MIME type (jpg, png, webp accepted; gif rejected). Repeated uploads for the same lang_code overwrite. The static mount on `/videos/` already covers this directory transparently.

**Alternative considered:** `OUT_DIR/{slug}/poster-{lang_code}.jpg` (no subdirectory). Rejected because `poster/` cleanly groups per-drama posters and makes deletion (rmtree) atomic.

### Decision: drama deletion (in `drama-entity` capability) cleans up translations and poster files

The drama-delete endpoint (added in `drama-as-entity`) already `rmtree`s `OUT_DIR/{slug}/`, which transitively removes `OUT_DIR/{slug}/poster/`. It must additionally `DELETE FROM translations WHERE entity_type='drama' AND entity_id=slug`. This is a small extension to the existing handler; tasked accordingly.

### Decision: drama-create form keeps the same shape

`POST /admin/dramas` continues to accept `drama_slug`, `drama_name`, `default_lang`. The handler now writes the name to translations atomically with the dramas row insert (transactional). Existing form / clients see no shape change. Synopsis is **not** accepted at create time; it is added per-language via the translation upsert endpoint (keeps the form short; synopsis is often written later anyway).

### Decision: `PUT /admin/dramas/{slug}/translations/{lang_code}` upserts both `name` and `synopsis`

Body: `{"name"?: "...", "synopsis"?: "..."}`. At least one field must be present. Each present field is upserted as a separate translation row; absent fields are not touched. This keeps the wire small ("update just the synopsis without re-sending name") and is a familiar PATCH-style semantic.

For a fresh language (no rows yet), `name` is required (otherwise the language has no resolvable name and is half-populated). The handler enforces: if the drama has no `name` translation in this `lang_code` yet, the body must include `name`. If the body sets only `name` without `synopsis`, that's fine — synopsis stays absent.

### Decision: `DELETE /admin/dramas/{slug}/translations/{lang_code}` removes the whole language entry

Removes name + synopsis + poster (file + translation row) for that lang_code. Same default-lang guard: 409 if `lang_code = default_lang`.

The poster file under `OUT_DIR/{slug}/poster/{lang_code}.*` is removed; failures log warnings into the response body's `warnings` array, consistent with episode/drama deletion conventions.

### Decision: `PATCH /admin/dramas/{slug}` retargets `default_lang` only

Same shape as `PATCH /admin/tags/{slug}` and `/admin/actors/{slug}`. Body: `{"default_lang": "..."}`. New default must (a) reference an active language and (b) have a `name` translation already (i.e. the drama is "complete" in the new default lang). 400 otherwise.

### Decision: poster upload via dedicated endpoint, not the translation upsert

Posters are binary; mixing JSON text fields with multipart bytes is awkward. Separate endpoint: `POST /admin/dramas/{slug}/poster?lang={code}` accepting a single `file` part with MIME `image/jpeg`, `image/png`, or `image/webp`. The handler:
1. Validates the drama and lang_code.
2. Determines extension from MIME.
3. Writes file to `OUT_DIR/{slug}/poster/{lang_code}.{ext}` (overwriting any prior).
4. Removes any prior poster file for this `(slug, lang_code)` with a different extension (e.g. switching from jpg to png).
5. Upserts translation row `(entity_type='drama', entity_id=slug, lang_code, field='poster', value='/videos/{slug}/poster/{lang_code}.{ext}')`.

Repeated uploads overwrite. `DELETE /admin/dramas/{slug}/poster?lang={code}` removes the file and the translation row.

### Decision: HTTP layer summary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/dramas` | (Modified) name → translations row instead of dramas.name. |
| `PATCH` | `/admin/dramas/{slug}` | JSON `{"default_lang"}`. New default must have a `name` translation. |
| `GET` | `/admin/dramas/{slug}/translations` | All translation rows for this drama, grouped by lang_code: `[{lang_code, name?, synopsis?, poster?}]`. |
| `PUT` | `/admin/dramas/{slug}/translations/{lang_code}` | JSON `{"name"?, "synopsis"?}`. Upsert. First-time entry requires `name`. |
| `DELETE` | `/admin/dramas/{slug}/translations/{lang_code}` | 409 if default. Removes name + synopsis + poster for this lang. |
| `POST` | `/admin/dramas/{slug}/poster?lang={code}` | Multipart `file`. Replaces poster for that lang. |
| `DELETE` | `/admin/dramas/{slug}/poster?lang={code}` | Removes poster file + translation row. |

## Risks / Trade-offs

- **Risk: drama deletion must clean up `translations` rows.** No FK on `translations.entity_id`. → Drama-delete handler explicitly issues `DELETE FROM translations WHERE entity_type='drama' AND entity_id=slug`. Tasked.
- **Risk: poster file orphans if translation row delete fails after disk write succeeds.** → Order: write file first, then INSERT translation; if INSERT fails, delete file. For DELETE: remove translation first, then file; if file delete fails, log warning but proceed.
- **Risk: switching default_lang loses access to the old language's posters / synopsis.** Doesn't actually — those rows remain; only the "fallback when no requested lang" changes. Verified in the design's PATCH semantics.
- **Trade-off: GET /admin/dramas/{slug}/translations returns all fields nested per lang.** Slightly more complex shape than `[{lang_code, field, value}, ...]` but matches the admin UI's per-language editor mental model.

## Migration Plan

No production data; destructive recreate of `hls.db`.

1. Stop server.
2. Delete `hls.db`.
3. Deploy new code; `init_db()` creates the schema with no `dramas.name` column.
4. Re-create languages, then dramas (POST `/admin/dramas` works; name routes to translations under default_lang).

## Open Questions

- Should `GET /admin/dramas` (admin list) include synopsis / poster info per drama, or only the name and counts? Leaning **only name + episode count + tag/actor counts** — keeping list responses lean. The drama-detail page (step 4) loads full translations on demand.
- Should poster have a maximum file size or dimension constraint? Defer to step 4 / `admin-redesign` when the upload UI is built; for now the handler accepts whatever the multipart upload size limit allows.
