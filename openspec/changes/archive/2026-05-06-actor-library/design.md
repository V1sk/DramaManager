## Context

`actor-library` is a structural mirror of `tag-library` (step 3a): same translation pattern, same junction-table topology, same admin and SDK surface — only the entity name and the translated field name differ. The decisions here are deliberately copied so the code paths can share helpers wherever practical (typed exceptions, validators, "upsert translation" logic).

## Goals / Non-Goals

**Goals:**
- An `actors` table keyed by `slug`, with `default_lang` for fallback semantics.
- A `drama_actors` junction enforcing referential integrity in both directions.
- Translation upsert / delete for actor names via the generic `translations` table.
- Admin CRUD HTTP surface for the actor library and drama-actor assignment.
- SDK `GET /api/actors` exposing every actor's slug + default-lang name.

**Non-Goals:**
- No "role" / "character name" field. Actors are flat name records; per-drama character-name attribution is a future change if needed.
- No `?lang=` query resolution for the SDK endpoint. Step 6 owns locale resolution.
- No drama-side actor projection (`DramaSummary.actors`). Step 6.

## Decisions

The following decisions mirror `tag-library` exactly. See `openspec/changes/tag-library/design.md` for rationale.

### Decision: `actors.slug` is operator-picked

Same regex as drama and tag slugs: `^[a-z0-9][a-z0-9-]*$`. ASCII-only, URL-safe, stable across staging/prod sync.

### Decision: `drama_actors` uses `ON DELETE CASCADE` on both sides

Junction rows carry no metadata; cascade is safe.

### Decision: drama–actor set is replaced wholesale via `PUT /admin/dramas/{slug}/actors`

Body is a JSON array of actor slugs. Idempotent.

### Decision: per-actor translation endpoint upserts a single language

`PUT /admin/actors/{slug}/translations/{lang_code}` body `{"name": "..."}` upserts `(entity_type='actor', entity_id=slug, lang_code, field='name')`. Default-lang translation cannot be deleted while the actor exists.

### Decision: `PATCH /admin/actors/{slug}` only changes `default_lang`

Slug is immutable. New default must reference an active language and an existing translation row.

### Decision: HTTP layer

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/actors` | Form `slug`, `default_lang`, `name`. Atomic insert + initial translation. 409 if slug exists. |
| `GET` | `/admin/actors` | All actors with `default_lang`, `default_name`, `available_langs`, `usage_count`. |
| `PATCH` | `/admin/actors/{slug}` | JSON `{"default_lang"}`. New default must exist as translation. |
| `DELETE` | `/admin/actors/{slug}` | Removes translations + actor row; cascades junction. 204. |
| `PUT` | `/admin/actors/{slug}/translations/{lang_code}` | JSON `{"name"}`. Upsert. |
| `DELETE` | `/admin/actors/{slug}/translations/{lang_code}` | 409 if `lang_code = default_lang`. |
| `PUT` | `/admin/dramas/{slug}/actors` | JSON array of actor slugs. Replace drama's actor set. |
| `GET` | `/admin/dramas/{slug}/actors` | `[{slug, name}]` localized to default_lang. |
| `GET` | `/api/actors` | SDK: `[{slug, name}]` localized to each actor's default_lang. |

## Risks / Trade-offs

- **Risk: `translations` rows for deleted actors can leak.** Same mitigation as tags: actor delete handler explicitly removes `WHERE entity_type='actor' AND entity_id=slug` before the actor row.
- **Trade-off: shared helper code with `tag-library`.** The two changes can either duplicate code or extract a generic `_upsert_named_translation(entity_type, ...)`. Decision deferred to implementation; tasks list both options.

## Open Questions

- Should there be a "person photo" upload per actor (with multi-language overlay possibilities)? Not in the user's stated requirements; deferred to a future change if needed.
