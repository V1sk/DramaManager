## Context

`i18n-foundation` provides a `languages` registry and a generic `translations` table; `drama-as-entity` provides the `dramas` row. Tags fit between them: each tag is a curated category whose label is translatable, and any number of dramas can carry any number of tags. This change is structurally near-identical to `actor-library` (step 3b); the patterns established here apply there.

## Goals / Non-Goals

**Goals:**
- A `tags` table keyed by `slug`, with `default_lang` for fallback semantics.
- A `drama_tags` junction enforcing referential integrity in both directions.
- Translation upsert / delete via the generic `translations` table.
- Admin CRUD HTTP surface for the tag library and drama-tag assignment.
- SDK `GET /api/tags` exposing every tag's slug + default-lang label.

**Non-Goals:**
- No `?lang=` query resolution for the SDK endpoint. Step 6 owns locale resolution.
- No drama-side tag projection (`DramaSummary.tags`). Step 6.
- No "add this tag to this drama" affordance separate from set replacement. The PUT-replace pattern is intentionally simple.

## Decisions

### Decision: `tags.slug` is operator-picked, not auto-generated from a label

Tags need a stable identifier across staging and prod (and potentially across renames). Auto-generating a slug from the default-lang label (`'Sci-Fi' → 'sci-fi'`) would tie the slug to whichever language the operator happened to type first; renaming the label later would either drift the slug or require migration. Picking the slug at create time is awkward but stable.

The slug regex matches `dramas.slug`: `^[a-z0-9][a-z0-9-]*$`. ASCII-only, URL-safe.

### Decision: `drama_tags` uses `ON DELETE CASCADE` on both sides

Deleting a drama removes its tag associations; deleting a tag removes the associations from every drama. Cascade is safe here because the junction row carries no information beyond the FK pair — there is nothing to "review" before removal. Compare with `episodes.drama_slug` (RESTRICT) where cascading would silently destroy media.

### Decision: drama–tag set is replaced wholesale via `PUT /admin/dramas/{slug}/tags`

Body is a JSON array of tag slugs, e.g. `["urban", "sci-fi"]`. The handler replaces the drama's tag set with exactly that set (computes diff internally; the wire is just "the new state"). Idempotent and matches the natural admin-UI flow ("multi-select then save").

**Alternative considered:** `POST /admin/dramas/{slug}/tags/{tag_slug}` and `DELETE …` for incremental ops. Rejected because it forces the UI to track diffs and emit one request per change.

### Decision: per-tag translation endpoint upserts a single language

`PUT /admin/tags/{slug}/translations/{lang_code}` body `{"label": "..."}` upserts the row `(entity_type='tag', entity_id=slug, lang_code=lang_code, field='label')`. Validation: `label` non-empty after trim. The endpoint accepts either an existing or missing row; `INSERT ... ON CONFLICT(...) DO UPDATE`.

`DELETE /admin/tags/{slug}/translations/{lang_code}` removes the row. The default-lang translation cannot be deleted while the tag exists (would orphan the tag's label); the handler returns 409 in that case. To remove the default-lang label, change the tag's default_lang first (PATCH endpoint added below) or delete the tag.

### Decision: `PATCH /admin/tags/{slug}` only changes `default_lang`

Slug is immutable (same posture as `languages.code`). The only mutable property at the tag-row level is `default_lang`. The PATCH endpoint requires the new `default_lang` to be a language with `is_active=1` AND a translation row for that lang already exists (otherwise switching default would leave the tag with no resolvable label).

### Decision: `GET /api/tags` returns default-lang labels for every tag

Response shape: `[{"slug": "urban", "label": "都市"}, ...]`. The label is the value of the translation `(entity_type='tag', entity_id=slug, lang_code=tag.default_lang, field='label')`. SDK gets a coherent, fallback-resolved list without needing locale negotiation. When step 6 introduces `?lang=`, the resolution becomes "requested lang → tag.default_lang"; today the chain has just one rung.

### Decision: HTTP layer

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/tags` | Form `slug`, `default_lang`, `label`. Creates the tag row + an initial `(slug, default_lang, 'label', label)` translation. 409 if slug exists. |
| `GET` | `/admin/tags` | All tags with their default-lang label and an array of `available_langs` (which translations exist). |
| `PATCH` | `/admin/tags/{slug}` | JSON `{"default_lang": "..."}`. New default must exist as a translation row for this tag. |
| `DELETE` | `/admin/tags/{slug}` | Cascades drama_tags + translations. 204. 404 if missing. |
| `PUT` | `/admin/tags/{slug}/translations/{lang_code}` | JSON `{"label": "..."}`. Upsert. |
| `DELETE` | `/admin/tags/{slug}/translations/{lang_code}` | 409 if `lang_code = tag.default_lang`. |
| `PUT` | `/admin/dramas/{slug}/tags` | JSON array of tag slugs. Replaces drama's tag set. 404 if drama missing. 400 if any tag slug missing. |
| `GET` | `/admin/dramas/{slug}/tags` | JSON array of `{slug, label}` for the drama's tags. |
| `GET` | `/api/tags` | SDK: `[{slug, label}]` localized to each tag's default_lang. |

## Risks / Trade-offs

- **Risk: `translations` rows for deleted tags can leak.** `tags` cascade-deletes rows in `drama_tags` but does not cascade into `translations` (no FK from translations to tags — they're typed-loosely). → The tag delete handler explicitly issues `DELETE FROM translations WHERE entity_type='tag' AND entity_id=slug` before deleting the tag row.
- **Trade-off: requiring a default-lang translation at create time means the create form has three required fields.** → Acceptable; matches the drama-creation form pattern from step 1.
- **Risk: PUT replacement of drama-tags ignores partial failures.** If one tag in the array doesn't exist, the entire PUT is rejected (no partial commit). → Correct behavior; admin UI surfaces the error.

## Open Questions

- Should `GET /admin/tags` include the count of dramas using each tag? Useful for admin UX but trivially derivable from a separate query if not. Will include it as `usage_count`.
- Should `GET /api/tags` include `available_langs` so SDK can know what translations exist? Defer to step 6 — today step 6 owns locale negotiation.
