## Why

After the data-layer changes (`drama-as-entity`, `i18n-foundation`, `tag-library`, `actor-library`, `drama-meta-translations`, `episode-subtitles`), every backend capability the operator needs exists, but the UI is six minimal half-pages bolted onto a single `admin.html` template. Operators currently have to: type slugs into raw forms, remember `ep_number` themselves, hit `/admin/tags` to manage tags then `/admin/actors` for actors, with no consolidated drama-detail view that shows synopsis + tags + actors + posters + episodes + subtitles together. They also can't preview an episode in-page (must copy the m3u8 URL into VLC or hls.js elsewhere), making the "审核 before sync" workflow established for step 6 awkward.

This change replaces the entire admin surface with a small SPA-flavored multi-page app: drama cards homepage, dedicated create-drama / drama-detail / episode-detail pages, and polished library pages for tags / actors / languages. It also introduces the concrete UX patterns that step 6 (`business-server-sync`) will hook into — the drama detail page is where the "同步整部剧" button will live; the episode detail page is where "同步本集" will live; the dirty/clean badges have a designated slot.

The auto-increment-ep-number flow ("operator picks a video file; server assigns the next episode number") becomes possible here because the drama-detail page knows the slug and can surface the next-ep affordance contextually — which step 1 deliberately deferred.

## What Changes

- **BREAKING** Replace `POST /admin/upload` (form fields `drama_slug`, `ep_number`, `video`) with two purpose-built endpoints:
  - `POST /admin/dramas/{slug}/episodes` — multipart `video` only; server computes `ep_number = MAX(ep_number) + 1` for that drama (concurrency-safe via UNIQUE-violation retry).
  - `POST /admin/dramas/{slug}/episodes/{ep}` — multipart `video`; targets a specific existing episode for re-encoding (overwrites in place; rejected with 409 if `status=encoding`).
- Replace the single-page admin template with a base layout (shared nav: 首页 / 标签 / 演员 / 语言) and dedicated routes:
  - `GET /admin` — drama cards homepage (poster + drama name + synopsis preview + epCount + "更新到第 N 集").
  - `GET /admin/dramas/new` — create-drama page (slug + default_lang + name + synopsis + poster + tags multi-select + actors multi-select); on success redirects to the drama detail page.
  - `GET /admin/dramas/{slug}` — drama detail (header with poster + name + synopsis; per-language tabs for translations & posters; tags & actors editor; episodes table with "上传下一集" affordance; delete-drama button gated on episode count).
  - `GET /admin/dramas/{slug}/episodes/{ep}` — episode detail (embedded hls.js player; subtitle list/upload/delete; video re-upload; cover replacement; episode delete).
  - Polished `GET /admin/tags`, `GET /admin/actors`, `GET /admin/languages` pages reusing the same nav + table + modal patterns.
- Add a single aggregate read endpoint `GET /admin/dramas/{slug}/full` returning drama row + translations grouped by lang + tags + actors + episodes (admin shape) in one payload, so the drama-detail page renders without N round-trips.
- Embed `hls.js` (CDN) in the episode detail template; player auto-attaches to `playUrl`; AES-128 key is fetched relative to the m3u8 (transparent to hls.js).
- Add server-side serialized retry on `ep_number` UNIQUE violations during auto-increment (worst case: 3 retries, then 503 with a "try again" hint).
- Move the delete-episode button from the legacy episode list row to the new episode detail page (and offer it on the drama detail page's per-row episodes table).

## Capabilities

### New Capabilities

- `admin-redesign`: the page tree, navigation, drama cards homepage, drama-detail page (translations editor, tags editor, actors editor, episodes table, episode upload affordances), episode-detail page (hls.js player, subtitle / video / cover re-upload, delete), aggregate read endpoint `/admin/dramas/{slug}/full`, and the new episode upload endpoints (`POST /admin/dramas/{slug}/episodes`, `POST /admin/dramas/{slug}/episodes/{ep}`).

### Modified Capabilities

- `hls-management-server`: removes `POST /admin/upload`; replaces "Admin web page" with the new page tree's home; admin-list / static-mount / DRM-key / persistence / pipeline-invocation requirements unchanged.
- `episode-deletion`: the "管理页删除 UI" requirement moves the delete button from the legacy single-page list to the new drama-detail and episode-detail pages.

## Impact

- **Code**: large `app/templates/` rewrite (`base.html`, `home.html`, `drama_new.html`, `drama_detail.html`, `episode_detail.html`, polished `tags.html` / `actors.html` / `languages.html`), new `app/static/` directory for shared CSS + minor JS helpers (or all-inline — TBD in design), new router routes (the bulk in `app/routers/admin.py` plus drama-scoped episode endpoints), aggregate read helper in `app/db.py`, removal of the legacy upload route.
- **Schema**: none. All data-layer work is already done in steps 1–3d.
- **External contracts**: none. SDK-facing endpoints (`/api/dramas`, `/api/episodes/...`) are untouched. The CLI / curl reproduction lines in CLAUDE.md need to update upload examples.
- **External dependency**: hls.js loaded from CDN (no Python dep added). If the operator network blocks public CDN, hls.js can be vendored under `static/`; the design documents both options.
- **Downstream**: step 5 (`oss-staging-prod-separation`) and step 6 (`business-server-sync`) plug into the drama-detail / episode-detail pages — sync-status badge slot and "同步" buttons are pre-allocated in step 4's templates and wired up by step 6.
