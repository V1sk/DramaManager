## ADDED Requirements

### Requirement: shared admin layout and navigation

The service SHALL serve a shared base HTML layout for every admin page (`/admin`, `/admin/dramas/...`, `/admin/tags`, `/admin/actors`, `/admin/languages`). The layout SHALL include a top navigation bar with links to: 首页 (`/admin`), 标签 (`/admin/tags`), 演员 (`/admin/actors`), 语言 (`/admin/languages`). The current page's nav link SHALL be visually highlighted.

The layout SHALL load shared CSS at `/static/admin.css` and shared JS at `/static/admin.js` (helpers: `fetchJSON(url, opts)`, `escapeHtml(s)`, `confirmDanger(message)`).

The layout SHALL include a `<div id="sync-zone">` element in the nav bar's right side, intentionally empty in this change, reserved for the step-6 `business-server-sync` sync UI.

#### Scenario: every admin page renders the shared nav
- **WHEN** the client requests any admin page (`/admin`, `/admin/dramas/new`, `/admin/dramas/{slug}`, `/admin/dramas/{slug}/episodes/{ep}`, `/admin/tags`, `/admin/actors`, `/admin/languages`)
- **THEN** the response HTML contains a top nav with anchors to `/admin`, `/admin/tags`, `/admin/actors`, `/admin/languages`
- **AND** the link matching the current page carries a CSS class indicating "active" / "current"

#### Scenario: shared CSS and JS are served from /static
- **WHEN** the client requests `/static/admin.css` or `/static/admin.js`
- **THEN** the response is 200 with the file contents and an appropriate `Content-Type`

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
- **GIVEN** `languages` has zero `is_active=1` rows
- **WHEN** the client requests `GET /admin/dramas/new`
- **THEN** the response HTML disables the submit button
- **AND** displays a message linking to `/admin/languages`

#### Scenario: full-flow creation succeeds and navigates to detail
- **GIVEN** at least one active language; a tag `urban`, an actor `zhang-san`
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
1. **Header strip**: poster image (per-language switchable; default = drama.default_lang), drama name (in default_lang), tags as badges, actors as a comma-separated list, synopsis (in default_lang).
2. **Action buttons in the header**: "编辑翻译", "编辑标签", "编辑演员", "删除剧" (the last is disabled when the episodes table has any rows). A reserved "[同步整部剧]" slot is rendered as a disabled placeholder button (functional in step 6).
3. **Episodes section**: a table of episodes ordered by `ep_number ASC`. Columns: episode number, cover thumbnail, status badge, duration, last updated, actions ("详情" link to episode detail page; "删除" calling existing `DELETE /admin/episodes/{slug}/{ep}` after confirm; an empty `<span class="sync-badge" data-ep="{n}">` reserved for step 6). A "[上传下一集]" button above the table opens a file picker that POSTs to the new `POST /admin/dramas/{slug}/episodes` endpoint and refreshes the page.
4. **Inline editor panels** (collapsed by default): clicking "编辑翻译" expands a per-language list of `name` / `synopsis` text inputs and a "上传海报" file picker, with save/delete buttons calling the existing translation / poster endpoints. A "+ 添加语言" affordance opens a sub-form requiring at least `name`. Clicking "编辑标签" / "编辑演员" expands a multi-select that calls `PUT /admin/dramas/{slug}/tags` / `actors` on save.

If the drama is unknown the response SHALL be 404 with an HTML error page.

#### Scenario: detail page renders aggregate data without client-side fetches
- **GIVEN** drama `ly` exists with translations, tags, actors, and 3 episodes
- **WHEN** the client requests `GET /admin/dramas/ly`
- **THEN** the response is 200 HTML containing all of: drama name, synopsis, poster URL, tag badges, actor list, episodes table (3 rows)
- **AND** the page DOES NOT need to call `/admin/dramas/ly/full` or other JSON endpoints to render the initial view (server-rendered)

#### Scenario: delete-drama button is disabled when episodes exist
- **GIVEN** drama `ly` has 1 or more rows in `episodes`
- **WHEN** the page renders
- **THEN** the "删除剧" button has the `disabled` attribute and a tooltip explaining the precondition

#### Scenario: delete-drama button enabled when no episodes
- **GIVEN** drama `gone` has zero rows in `episodes`
- **WHEN** the page renders
- **THEN** the "删除剧" button is enabled
- **AND** clicking it shows a confirm dialog
- **AND** confirming calls `DELETE /admin/dramas/gone` and on 200 navigates back to `/admin`

#### Scenario: upload-next-episode button auto-increments
- **GIVEN** drama `ly` has 5 ready episodes
- **WHEN** the operator picks a video file and submits via "[上传下一集]"
- **THEN** the browser POSTs to `/admin/dramas/ly/episodes` with the file
- **AND** on success the page reloads
- **AND** the episodes table now contains a 6th row with `ep_number=6` and `status` initially `pending` or `encoding`

#### Scenario: unknown drama returns 404
- **WHEN** the client requests `GET /admin/dramas/never-seen`
- **THEN** the response is 404 with an HTML error page

### Requirement: episode detail page with embedded player

The service SHALL serve `GET /admin/dramas/{slug}/episodes/{ep}` returning an HTML page with:
1. **Embedded video player**: a `<video controls>` element initialized via hls.js (loaded from CDN per the design doc). The player MUST attach to the episode's `play_url` (current 720p media playlist). For browsers that natively support HLS (Safari / iOS), native playback SHALL be used as a fallback when `Hls.isSupported()` is false.
2. **Subtitle tracks**: every row in `subtitles` for this episode SHALL be rendered as a `<track kind="subtitles" src="{url}" srclang="{lang_code}" label="{label}">` inside the `<video>` element. The `<video>` SHALL carry the `crossorigin` attribute so the browser fetches subtitles with CORS.
3. **Episode metadata**: episode number, status, duration, source filename, source dimensions.
4. **Cover replacement**: a thumbnail of the cover with a "更换封面" file picker calling existing `POST /api/episodes/{slug}/{ep}/cover`.
5. **Video re-upload**: a "[重传视频]" file picker calling new `POST /admin/dramas/{slug}/episodes/{ep}` (re-encode existing). After submission the page polls `/admin/episodes` every 2 seconds until status leaves `encoding`, then refreshes.
6. **Subtitle management**: a list of present subtitles (lang_code, label, file URL preview, uploaded_at) each with "替换" and "删除" buttons calling existing subtitle endpoints. A "+ 添加字幕" form below the list with a language dropdown (sourced from `/api/languages`, **excluding** languages already present) and a file picker calling `POST /admin/episodes/{slug}/{ep}/subtitles?lang={code}`.
7. **Episode delete**: a "[删除本集]" button calling existing `DELETE /admin/episodes/{slug}/{ep}` after confirm; on 200, navigates back to `/admin/dramas/{slug}`.
8. A reserved "[同步本集]" slot rendered as a disabled placeholder (step 6 wires it up).

If the episode does not exist the response SHALL be 404 with an HTML error page.

#### Scenario: page renders embedded player and subtitle tracks
- **GIVEN** episode `ly-ep-3` is ready with `play_url='/videos/ly/ep-3/720p/media-720p.m3u8'` and subtitles in `en` and `zh-rCN`
- **WHEN** the client requests `GET /admin/dramas/ly/episodes/3`
- **THEN** the HTML response contains a `<video>` element
- **AND** an inline script attaches hls.js to that element with `playUrl='/videos/ly/ep-3/720p/media-720p.m3u8'`
- **AND** the `<video>` contains two `<track>` elements: one with `srclang="en" label="English"` and one with `srclang="zh-rCN" label="简体中文"`
- **AND** the `<video>` carries the `crossorigin` attribute

#### Scenario: re-upload triggers re-encode and the page polls for status
- **GIVEN** the same ready episode
- **WHEN** the operator picks a new video file and submits via "[重传视频]"
- **THEN** the browser POSTs to `/admin/dramas/ly/episodes/3`
- **AND** the page begins polling `/admin/episodes` every 2 seconds
- **AND** when the polled row's status transitions back to `ready` or `failed` the page reloads

#### Scenario: language picker for new subtitle excludes already-present languages
- **GIVEN** episode `ly-ep-3` has subtitle rows for `en` and `zh-rCN`
- **WHEN** the page renders the "+ 添加字幕" form
- **THEN** the language dropdown options exclude `en` and `zh-rCN`

#### Scenario: deleting the episode navigates back to the drama detail page
- **GIVEN** the user clicks "[删除本集]" and confirms
- **WHEN** the response is 200
- **THEN** the browser navigates to `/admin/dramas/{slug}`

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
