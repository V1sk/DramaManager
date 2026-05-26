# admin-redesign

操作员后台 UX 重做：共享 base 模板 + nav 栏；剧目卡片首页；剧详情 / 集详情独立页；嵌入式 hls.js 播放器；分集级上传 / 重传 / 删除；标签 / 演员 / 语言库页面统一布局。归档自 `admin-redesign`。

## Requirements

### Requirement: shared admin layout and navigation

The service SHALL serve a shared base HTML layout for every admin page (`/admin`, `/admin/dramas/...`, `/admin/tags`, `/admin/actors`, `/admin/languages`, `/admin/sync`). The layout SHALL include a top navigation bar with links to: 首页 (`/admin`), 标签 (`/admin/tags`), 演员 (`/admin/actors`), 语言 (`/admin/languages`). The current page's nav link SHALL be visually highlighted.

The layout SHALL load shared CSS at `/static/admin.css` and shared JS at `/static/admin.js`.

The nav bar SHALL include a `<div id="sync-zone">` element on its right side. When `BUSINESS_SYNC_BASE_URL` is configured at startup, this element SHALL render a small `需同步: N` link to `/admin/sync` where N is the count of drama + episode rows whose `sync_status` is **not** `clean`. When N=0 the link reads `已同步` (clean). When `BUSINESS_SYNC_BASE_URL` is not configured, the element stays empty.

The page SHALL refresh the sync-zone count every 5 seconds via `GET /admin/sync/summary` (returns `{"non_clean_count": N}`).

#### Scenario: sync zone shows count when sync is configured
- **GIVEN** `BUSINESS_SYNC_BASE_URL` is set; 2 dramas dirty, 1 episode sync_failed
- **WHEN** the operator opens any admin page
- **THEN** the nav bar's sync zone shows `需同步: 3` linking to `/admin/sync`

#### Scenario: sync zone empty when sync disabled
- **GIVEN** `BUSINESS_SYNC_BASE_URL` is not set
- **WHEN** the operator opens any admin page
- **THEN** the sync zone div renders empty (no link, no text)

### Requirement: drama cards homepage

The service SHALL serve `GET /admin` returning an HTML page that lists every drama as a card. Each card SHALL display: the drama's poster image (default-language poster, fallback to a placeholder if absent), the drama's name (in default_lang), a synopsis preview (first ~80 characters of default_lang synopsis, or empty if none), the latest ready episode number ("更新到第 N 集" or "暂无已就绪集" if none), and total ready episode count.

Cards SHALL be ordered by: dramas with at least one ready episode first, ordered by `MAX(episodes.updated_at) DESC`; then dramas with zero ready episodes by `dramas.created_at DESC`. Each card SHALL be a link / clickable element navigating to `/admin/dramas/{slug}`.

The page SHALL include a prominent "+ 创建短剧" button linking to `/admin/dramas/new`. When the `dramas` table is empty the page SHALL show an empty state with instructions and the same create button.

#### Scenario: dramas with episodes appear before empty dramas
- **GIVEN** drama `a` has 2 ready episodes (`MAX(updated_at)='2026-04-22'`); drama `b` has 0 episodes (created `2026-04-23`); drama `c` has 1 ready episode (`MAX(updated_at)='2026-04-24'`)
- **WHEN** the client requests `GET /admin`
- **THEN** the cards appear in order `[c, a, b]`

#### Scenario: card content is sourced from default-lang translations
- **GIVEN** drama `ly` with `default_lang='zh-rCN'` and translations: name=琅琊榜, synopsis=豪门复仇..., poster=/videos/ly/poster/zh-rCN.jpg; 5 ready episodes, latest ep_number=5
- **WHEN** the client requests `GET /admin`
- **THEN** the card for `ly` shows the Chinese name, the truncated Chinese synopsis, the Chinese poster, "更新到第 5 集"

#### Scenario: empty dramas show fallback strings
- **GIVEN** drama `new` with no episodes and no synopsis translation
- **WHEN** the client requests `GET /admin`
- **THEN** the card for `new` shows "暂无已就绪集" instead of "更新到第 N 集"
- **AND** the synopsis area is empty (no placeholder text)

### Requirement: create-drama page

The service SHALL serve `GET /admin/dramas/new` returning an HTML form with fields: `slug` (text), `default_lang` (select populated client-side from `GET /api/languages`), `name` (text), `synopsis` (textarea), `poster` (file input, optional), `tags` (multi-select populated from `GET /api/tags`), `actors` (multi-select populated from `GET /api/actors`).

When the active languages registry is empty the page SHALL disable the form and display a message linking the operator to `/admin/languages`.

The page's submit handler SHALL execute, in order, against the existing endpoints from prior changes:
1. `POST /admin/dramas` with `{slug, drama_name, default_lang}`. On 409 / 400 the form re-renders with the error and stops.
2. `PUT /admin/dramas/{slug}/translations/{default_lang}` with `{synopsis}` — only if synopsis is non-empty.
3. `POST /admin/dramas/{slug}/poster?lang={default_lang}` with the poster file — only if a poster was selected.
4. `PUT /admin/dramas/{slug}/tags` with the selected tag slugs — only if at least one is selected.
5. `PUT /admin/dramas/{slug}/actors` with the selected actor slugs — only if at least one is selected.
6. Navigate to `/admin/dramas/{slug}`.

Failures in steps 2–5 SHALL NOT block the navigation in step 6; instead the destination page SHALL display a flash message describing which step failed and prompting the operator to retry from the detail page. Step 1's failure halts the flow and re-renders the form.

#### Scenario: empty registry blocks create form
- **GIVEN** `languages` has zero rows
- **WHEN** the client requests `GET /admin/dramas/new`
- **THEN** the response HTML disables the submit button
- **AND** displays a message linking to `/admin/languages`

#### Scenario: full-flow creation succeeds and navigates to detail
- **GIVEN** at least one language; a tag `urban`, an actor `zhang-san`
- **WHEN** the client submits the form with slug=ly, default_lang=zh-rCN, name=琅琊榜, synopsis="...", poster file, tags=[urban], actors=[zhang-san]
- **THEN** all five POST/PUT calls succeed
- **AND** the browser navigates to `/admin/dramas/ly`
- **AND** the drama detail page renders with all submitted data populated

#### Scenario: partial failure surfaces as flash on detail page
- **GIVEN** the same form submission, but the poster upload fails (e.g. simulated 500)
- **WHEN** the browser orchestrator continues
- **THEN** the drama row, name translation, synopsis, tags, actors are all persisted (the steps that succeeded)
- **AND** the browser navigates to `/admin/dramas/ly` with a flash message naming the poster step
- **AND** the operator can re-attempt poster upload from the detail page

### Requirement: drama detail page

The service SHALL serve `GET /admin/dramas/{slug}` returning an HTML page that displays:
1. **Header strip**: poster image (per-language switchable; default = drama.default_lang), drama name (in default_lang), tags as badges, actors as a comma-separated list, synopsis (in default_lang), **a sync badge** showing the drama's `sync_status` rendered with a color: clean (green), dirty (yellow), syncing (blue with spinner), sync_failed (red, click to view `sync_error`), pending_delete (orange).
2. **Action buttons in the header**: "编辑翻译", "编辑标签", "编辑演员", "删除剧" (disabled when episodes table is non-empty), and **"[同步整部剧]"** which is **functional** when sync is configured: clicking POSTs to `/admin/dramas/{slug}/sync`. Disabled and labeled "已同步" when the drama is `clean` AND no child episode is `dirty`/`pending_delete`/`sync_failed`. Disabled with a config tooltip when sync is not configured.
3. **Episodes section**: a table of episodes ordered by `ep_number ASC` (excluding rows with `sync_status='pending_delete'` from the visible list — they show only on the sync overview page). Columns: episode number, cover thumbnail, status badge (pipeline status), **sync badge** (sync_status), duration, last updated, actions ("详情" link; "删除" calling existing `DELETE /admin/episodes/{slug}/{ep}` after confirm; per-row "[同步本集]" button when the row is non-clean and sync is configured). A "[上传下一集]" button above the table.
4. **Inline editor panels**: same as before. After save, the drama's sync_status flips to `dirty` (per the dirty-marking rules in `business-server-sync`); the page refreshes to reflect the new state.

If the drama is unknown the response SHALL be 404 with an HTML error page.

#### Scenario: detail page renders sync badges for drama and episodes
- **GIVEN** drama `ly` `dirty` with episodes 1 (`clean`), 2 (`dirty`), 3 (`sync_failed`)
- **WHEN** the operator opens `/admin/dramas/ly`
- **THEN** the page shows a yellow "需同步" badge in the drama header
- **AND** the episodes table shows a green badge for ep 1, yellow for ep 2, red for ep 3 (with click-to-view-error)
- **AND** the "[同步整部剧]" button is enabled

#### Scenario: detail page hides pending_delete episodes
- **GIVEN** drama `ly` with episodes 1 (`clean`), 2 (`pending_delete`)
- **WHEN** the operator opens `/admin/dramas/ly`
- **THEN** the episodes table shows only episode 1
- **AND** the operator can find episode 2 listed on `/admin/sync`

#### Scenario: editing drama dirties drama and the sync button activates
- **GIVEN** drama `ly` `clean`
- **WHEN** the operator changes the synopsis via the inline editor
- **AND** the page refreshes (or polls) drama state
- **THEN** the drama's sync badge becomes yellow (dirty)
- **AND** the "[同步整部剧]" button is enabled

#### Scenario: clicking sync transitions drama to syncing
- **GIVEN** drama `ly` `dirty` and sync configured
- **WHEN** the operator clicks "[同步整部剧]"
- **THEN** the page POSTs to `/admin/dramas/ly/sync`
- **AND** receives 202
- **AND** the drama's badge transitions to blue ("syncing") with a spinner
- **AND** the page polls `/admin/dramas/ly/full` every 2 seconds until the badge leaves `syncing`

### Requirement: episode detail page with embedded player

The service SHALL serve `GET /admin/dramas/{slug}/episodes/{ep}` returning an HTML page with the same elements as before (embedded hls.js player, subtitle tracks, metadata, cover replacement, video re-upload, subtitle management, episode delete) plus:

5. **Sync badge** showing the episode's `sync_status` next to the metadata header.
6. **"[同步本集]" button** in the page header. Functional when sync is configured: POSTs to `/admin/episodes/{slug}/{ep}/sync`. Disabled when:
   - The episode is `clean` (label: "已同步").
   - The drama has never been synced (label: "先同步剧"; the button links the operator to the drama page).
   - Sync is not configured.

The page SHALL poll `/admin/episodes` (existing endpoint, now returning `sync_status` per row) every 2 seconds while either the pipeline status is in `{pending, encoding}` or the sync_status is in `{syncing}`.

#### Scenario: episode page shows sync badge and active button
- **GIVEN** ready episode `ly-ep-3` with `sync_status='dirty'`; drama `ly` `last_synced_at` set
- **WHEN** the operator opens the episode detail page
- **THEN** a yellow sync badge appears
- **AND** the "[同步本集]" button is enabled

#### Scenario: episode sync button disabled when drama never synced
- **GIVEN** episode `ly-ep-3` `dirty`; drama `ly` `last_synced_at IS NULL`
- **WHEN** the operator opens the episode detail page
- **THEN** the "[同步本集]" button is disabled with label "先同步剧"

### Requirement: episode upload endpoints (auto-increment + re-upload)

The service SHALL provide `POST /admin/dramas/{slug}/episodes` accepting `multipart/form-data` with a single `video` part. The endpoint SHALL behave as follows:

1. Validate `slug` matches `^[a-z0-9][a-z0-9-]*$` (else 422). The drama must exist (else 404).
2. Stream the upload to a temporary file under `UPLOAD_TMP_DIR`.
3. Run ffprobe (`duration_ms`, `width`, `height`) and first-frame extraction (cover) — same as the legacy upload flow.
4. Compute `next_ep = SELECT COALESCE(MAX(ep_number), 0) + 1 FROM episodes WHERE drama_slug=?`.
5. Insert the new `episodes` row with that `ep_number` and `episode_id="{slug}-ep-{next_ep}"`. On UNIQUE violation, retry from step 4 up to 2 more times (3 total attempts).
6. Enqueue the pipeline job. Respond 302 to `/admin/dramas/{slug}`.

If 3 attempts collide on UNIQUE the response SHALL be 503 with a body explaining "concurrent upload collision; retry."

The service SHALL provide `POST /admin/dramas/{slug}/episodes/{ep}` accepting `multipart/form-data` with a single `video` part. The endpoint SHALL behave as follows:

1. Validate `slug` and `ep` patterns (else 422). Episode `(slug, ep)` must exist (else 404).
2. If the episode's current `status='encoding'`, respond 409 with a message indicating concurrent re-upload is not allowed.
3. Stream upload + ffprobe + cover extraction.
4. Update the existing episode row in place (status reset to `pending`, DRM fields cleared, new dimensions / duration / cover persisted, `updated_at` refreshed) — the existing `db.upsert_pending` already supports this overwrite.
5. Enqueue the pipeline job. Respond 302 to `/admin/dramas/{slug}/episodes/{ep}`.

#### Scenario: auto-increment assigns the next number
- **GIVEN** drama `ly` has episodes 1, 2, 3 (regardless of statuses)
- **WHEN** the operator POSTs a video to `/admin/dramas/ly/episodes`
- **THEN** the response is 302 to `/admin/dramas/ly`
- **AND** a new episode row exists with `(drama_slug='ly', ep_number=4, status='pending')`
- **AND** the pipeline job for ep-4 is enqueued

#### Scenario: re-upload to existing episode
- **GIVEN** episode `ly-ep-2` exists with `status='ready'`
- **WHEN** the operator POSTs a new video to `/admin/dramas/ly/episodes/2`
- **THEN** the existing row is updated in place (`status='pending'`, DRM cleared, dimensions/duration/cover refreshed)
- **AND** the response is 302 to `/admin/dramas/ly/episodes/2`
- **AND** the pipeline job is enqueued

#### Scenario: re-upload while encoding is rejected
- **GIVEN** episode `ly-ep-2` exists with `status='encoding'`
- **WHEN** the operator POSTs a new video to `/admin/dramas/ly/episodes/2`
- **THEN** the response is 409 with a message
- **AND** the row is unchanged
- **AND** the temporary upload file (if streamed) is deleted

#### Scenario: upload to non-existent drama is rejected
- **WHEN** the operator POSTs a video to `/admin/dramas/never-seen/episodes`
- **THEN** the response is 404
- **AND** no row is created
- **AND** the temporary upload file is deleted

### Requirement: aggregate drama-detail read endpoint

The service SHALL provide `GET /admin/dramas/{slug}/full` returning a single JSON payload consolidating:
- drama row: `slug`, `default_lang`, `created_at`, `updated_at`
- `translations`: object keyed by lang_code, each value `{name, synopsis, poster}` (each field nullable)
- `tags`: array of `{slug, label}` (label localized to each tag's default_lang)
- `actors`: array of `{slug, name}` (name localized to each actor's default_lang)
- `episodes`: array ordered by `ep_number ASC`, each `{ep_number, episode_id, status, duration_ms, width, height, cover_url, play_url, source_filename, error_message, subtitle_count, updated_at}`

If the drama is unknown the response is 404.

This endpoint SHALL be used by the drama-detail server-side template at render time (so the rendered HTML is fully populated without client-side fetches) and also by client-side JS for refreshing the page after edits without reloading.

#### Scenario: aggregate response includes everything needed by the detail page
- **GIVEN** drama `ly` with translations in `zh-rCN` and `en`, 2 tags, 1 actor, 3 episodes (each with 0–2 subtitles)
- **WHEN** the client requests `GET /admin/dramas/ly/full`
- **THEN** the response is 200 JSON containing all fields described above
- **AND** every episode element includes its `subtitle_count` (e.g. 0, 1, or 2)

### Requirement: polished library pages reuse the shared layout

The existing minimal pages introduced by `i18n-foundation` (`/admin/languages`), `tag-library` (`/admin/tags`), and `actor-library` (`/admin/actors`) SHALL be re-rendered using the shared base template, the shared CSS, and a consistent table + create-form pattern. Behavior of the underlying endpoints is unchanged (no new validation, no new endpoints in this requirement).

Each library page SHALL:
- Extend the shared base layout (nav highlighted on the matching link).
- Show a table of all rows (admin shape, including inactive items where applicable).
- Show an inline create form above the table.
- Provide row-level edit (PATCH) and delete (DELETE) actions consistent with each capability's API.

#### Scenario: each library page extends the base layout
- **WHEN** the client requests `/admin/tags`, `/admin/actors`, or `/admin/languages`
- **THEN** the response HTML extends the shared base, including the same nav bar
- **AND** the matching nav link carries the "active" CSS class

### Requirement: hls.js delivery

The service SHALL include hls.js in the episode detail page via a `<script>` tag. The default source SHALL be a public CDN URL (e.g. `https://cdn.jsdelivr.net/npm/hls.js@1.5/dist/hls.min.js`). An operator MAY override this by placing `hls.min.js` under `app/static/vendor/` and switching the include to `/static/vendor/hls.min.js`; the override path SHALL be documented as a commented-out line in the base template.

The page SHALL gracefully degrade when `Hls` is undefined: it SHALL fall back to native HLS playback (`video.src = playUrl`) for browsers that report `canPlayType('application/vnd.apple.mpegurl')` truthy.

#### Scenario: page works with hls.js loaded from CDN
- **GIVEN** the operator's network can reach the configured CDN
- **WHEN** the operator opens the episode detail page in Chrome
- **THEN** hls.js attaches and the video plays

#### Scenario: page falls back to native HLS in Safari
- **GIVEN** Safari (which does not need hls.js but where `Hls.isSupported()` returns false)
- **WHEN** the operator opens the episode detail page
- **THEN** the `<video>` element gets `src=playUrl` set directly
- **AND** native HLS playback works
