## 1. Static asset scaffolding

- [x] 1.1 Create `app/static/` directory. Mount it in `app/main.py` via `app.mount("/static", StaticFiles(directory="app/static"), name="static")`.
- [x] 1.2 Create `app/static/admin.css` with: page reset, top-nav styling, drama card grid, table styling, badge styles (status pending/encoding/ready/failed), modal/inline-panel styling, form layout. Migrate visual variables (colors, spacing) from the existing inline `<style>` in `templates/admin.html`.
- [x] 1.3 Create `app/static/admin.js` exporting helpers as window globals: `fetchJSON(url, opts={})` (wraps `fetch` with JSON body + error throw); `escapeHtml(s)`; `confirmDanger(message)`; `flash(message, kind='info')` (renders a top-of-page strip).
- [x] 1.4 Create `app/static/vendor/` (empty for now; documented as the optional fallback for hls.js in CLAUDE.md).

## 2. Shared base template

- [x] 2.1 Create `templates/_base.html` with: HTML skeleton, `<title>{% block title %}HLS 管理{% endblock %}</title>`, link to `/static/admin.css`, top nav with anchors to 首页/标签/演员/语言, a `<div id="flash-zone">`, `<div id="sync-zone">` (empty), `{% block content %}{% endblock %}`, script include of `/static/admin.js`, optional `{% block scripts %}{% endblock %}`.
- [x] 2.2 Implement nav-active class via a `{% block nav_active %}{% endblock %}` slot or a small Jinja conditional driven by request path.
- [x] 2.3 Add a flash-message macro (`render_flash(message, kind)`) that other templates can call after redirects.

## 3. Drama cards homepage

- [x] 3.1 Add `db.list_dramas_for_homepage()` returning per-drama: slug, default_lang, default-lang `name`, default-lang `synopsis` (truncated to 80 chars), default-lang `poster` URL, episode count where status=ready, latest ready ep_number (or NULL), max(updated_at) over ready episodes (for sorting), drama.created_at. Use SQL with LEFT JOINs against `translations` and `episodes`.
- [x] 3.2 Sort: dramas with at least one ready episode first by `latest_ready_updated_at DESC`; then dramas with zero ready episodes by `created_at DESC`. Tie-break by `slug ASC`.
- [x] 3.3 Create `templates/home.html` extending `_base.html`, rendering the cards grid. Each card is an `<a>` linking to `/admin/dramas/{slug}` with the poster image (or a `<div>` placeholder when null), name, synopsis preview, latest-ep label, ep count.
- [x] 3.4 Replace the existing `GET /admin` handler in `app/routers/admin.py` to render `home.html` with the rows from 3.1.
- [x] 3.5 Add the "+ 创建短剧" link to `/admin/dramas/new` at the top of the cards area.

## 4. Create-drama page

- [x] 4.1 Add `GET /admin/dramas/new` returning `templates/drama_new.html` extending `_base.html`. The page contains the form skeleton (slug input, language select placeholder, name input, synopsis textarea, poster file input, two `<select multiple>` for tags and actors, submit button).
- [x] 4.2 Inline `<script>` (or per-page in `templates/drama_new.html`) on page load: fetch `/api/languages`, populate the language select; fetch `/api/tags`, populate the tags select; fetch `/api/actors`, populate the actors select. If the language list is empty, disable the form and show a flash CTA.
- [x] 4.3 Wire the submit handler to perform the 5-step orchestration described in the design. Show progress text ("creating drama…", "uploading poster…"). On step 1 failure, show the error and stop. On steps 2–5 failure, navigate to `/admin/dramas/{slug}` with a flash message naming the failed step.
- [x] 4.4 Test end-to-end with: empty registry case; full happy path; partial failure on poster step.

## 5. Drama detail page

- [x] 5.1 Add `db.get_drama_full(slug)` returning the aggregate shape from the spec (drama + translations + tags + actors + episodes). Single function, multiple SELECTs internally.
- [x] 5.2 Add `GET /admin/dramas/{slug}/full` JSON endpoint that calls 5.1. 404 if missing.
- [x] 5.3 Add `GET /admin/dramas/{slug}` rendering `templates/drama_detail.html` extending `_base.html`. Server-side template fills the page using 5.1's data inline. 404 if missing.
- [x] 5.4 Header strip: poster (default-lang), name, tags as badges, actors comma-separated, synopsis. A small dropdown next to the poster lets the operator switch which language's poster is displayed (client-side only, swaps the `<img>`); each option also has a small "上传海报" file picker.
- [x] 5.5 Action buttons: 编辑翻译, 编辑标签, 编辑演员, 删除剧, 同步整部剧 (disabled placeholder).
- [x] 5.6 Episodes table: rows with cover thumb, ep number, status badge, duration, updated_at, actions. Empty row state. Each row carries an empty `<span class="sync-badge" data-ep="{n}">` for step 6.
- [x] 5.7 "[上传下一集]" button: opens a hidden `<input type="file" accept="video/*">`; on file select, POSTs to `/admin/dramas/{slug}/episodes` and reloads.
- [x] 5.8 Inline editor panels (collapsed): per-language translation editor; tag multi-select; actor multi-select. Save calls `PUT /admin/dramas/{slug}/translations/{lang}`, `PUT /admin/dramas/{slug}/tags`, `PUT /admin/dramas/{slug}/actors` respectively. "+ 添加语言" sub-form requires `name`.
- [x] 5.9 Per-language poster upload (within translation editor or attached to header dropdown): POSTs to `/admin/dramas/{slug}/poster?lang={code}`. After success, refresh the affected `<img>`.
- [x] 5.10 "删除剧" button: disabled when `episodes.length > 0`. Enabled state: `confirm()` then `DELETE /admin/dramas/{slug}` then navigate to `/admin`.
- [x] 5.11 Periodic refresh: when any episode in the table has `status='encoding'` or `status='pending'`, the page polls `/admin/dramas/{slug}/full` every 5 seconds and updates only the episodes table. Stops polling when no transient statuses remain.

## 6. Episode detail page

- [x] 6.1 Add `GET /admin/dramas/{slug}/episodes/{ep}` rendering `templates/episode_detail.html` extending `_base.html`. Server fetches: episode row, drama row (for nav back), subtitles list (joined with `languages.display_label`). 404 if episode missing.
- [x] 6.2 Embedded player: `<video id="player" controls crossorigin>` with `<track>` elements for each subtitle. Inline script attaches hls.js to `play_url` if `Hls.isSupported()`, else sets `video.src = play_url` (Safari path).
- [x] 6.3 Episode metadata: ep number, status badge, duration formatted, source filename (if available), source dimensions (`{width}x{height}` or fallback). Sync placeholder slot.
- [x] 6.4 Cover replacement: thumbnail with "更换封面" file picker → POST `/api/episodes/{slug}/{ep}/cover` (existing endpoint).
- [x] 6.5 "[重传视频]" file picker: POST `/admin/dramas/{slug}/episodes/{ep}` (new endpoint). After 302 / 200, start polling `/admin/episodes` for this `episode_id`'s status; when not in `{pending, encoding}`, reload the page.
- [x] 6.6 Subtitle list rendering: a row per language with `lang_code`, `label`, file URL preview link (opens `.vtt` in new tab), `uploaded_at` formatted, "替换" + "删除" buttons.
- [x] 6.7 "+ 添加字幕" form: lang `<select>` (sourced from `/api/languages` excluding present ones), file picker. Submit → `POST /admin/episodes/{slug}/{ep}/subtitles?lang={code}` with multipart file → reload the subtitle list.
- [x] 6.8 "替换" subtitle: same endpoint, same lang_code. "删除" → DELETE existing endpoint, refresh list.
- [x] 6.9 "[删除本集]" button: confirm() → DELETE `/admin/episodes/{slug}/{ep}` → navigate to `/admin/dramas/{slug}`.
- [x] 6.10 (Optional) "[切换码率]" dropdown that swaps `playUrl` between 540p / 720p / 1080p for preview parity.

## 7. New episode upload endpoints

- [x] 7.1 In `app/routers/admin.py` add `POST /admin/dramas/{slug}/episodes` (multipart `video` only). Validate slug; ensure drama exists. Stream upload, run ffprobe + cover. Compute `next_ep` and INSERT in a retry loop (3 attempts on UNIQUE collision); 503 if all fail. On success enqueue pipeline job, 302 to `/admin/dramas/{slug}`.
- [x] 7.2 Add `POST /admin/dramas/{slug}/episodes/{ep}` (multipart `video`). Validate; ensure episode exists; reject 409 if `status='encoding'`. Stream upload, ffprobe + cover, `db.upsert_pending` (overwrite path), enqueue job, 302 to `/admin/dramas/{slug}/episodes/{ep}`.
- [x] 7.3 Remove the legacy `POST /admin/upload` route handler. Add a stub `405 Method Not Allowed` with a body explaining the new endpoint paths (or remove entirely; FastAPI returns 404 by default which is fine).
- [x] 7.4 Update the `Job` dataclass / queue invocation if needed (no behavior change expected — same per-job inputs).

## 8. Polished library pages

- [x] 8.1 Rewrite `templates/languages.html` to extend `_base.html`. Reuse styles from `admin.css`. Same endpoints as i18n-foundation.
- [x] 8.2 Rewrite `templates/tags.html` to extend `_base.html`. Same endpoints as tag-library; add the per-tag "manage translations" inline panel UX.
- [x] 8.3 Rewrite `templates/actors.html` similarly.
- [x] 8.4 Verify nav highlighting works on each page.

## 9. Aggregate read endpoint exercise

- [x] 9.1 Confirm `GET /admin/dramas/{slug}/full` is used by both server-side rendering and any client-side post-edit refreshes. Avoid duplicate queries.

## 10. Manual verification

- [x] 10.1 Boot the server with a fresh DB. Seed at least two languages, a tag, and an actor via the polished library pages.
- [x] 10.2 From `/admin`, click "+ 创建短剧"; fill all fields including poster + tags + actors; submit. End up on `/admin/dramas/{slug}` with everything populated.
- [x] 10.3 From the drama detail page, click "[上传下一集]"; pick a video; verify a new row appears with `ep_number=1`, status `pending` then `encoding` then `ready`; the episodes table polling updates the badge.
- [x] 10.4 Click "详情" on the ready episode; the page renders an embedded hls.js player; click play; verify video plays.
- [x] 10.5 Add a `.vtt` subtitle (one with valid `WEBVTT` magic). Reload the player; verify subtitle track appears in the player's CC menu.
- [x] 10.6 Re-upload the video for the same episode; verify status flips to encoding and the page polls until ready, then reloads.
- [x] 10.7 Delete the episode from the detail page; verify navigation back to the drama page; the episode row is gone.
- [x] 10.8 Upload another episode via "[上传下一集]" — verify it gets `ep_number=2` (gap from 1 is filled by re-using next number based on MAX, NOT by reusing deleted numbers — verify this matches design: `MAX(ep_number)+1` so if 1 was deleted before any others existed, the next gets 1 again; if 1 then 2, both deleted, next gets 1; if 1 ready then 2 deleted, next is `MAX(1)+1=2`).
- [x] 10.9 Delete all episodes; the "删除剧" button becomes enabled. Click it, confirm, and verify navigation back to `/admin` and the drama is gone from the cards grid.
- [x] 10.10 Visit `/admin/tags`, `/admin/actors`, `/admin/languages`; confirm the shared nav and consistent styling.

## 11. Documentation

- [x] 11.1 Update `CLAUDE.md`'s "Management server" section: replace the URL map's `POST /admin/upload` line with the two new per-drama endpoints. Document the new page tree. Note the optional `app/static/vendor/hls.min.js` fallback path.
- [x] 11.2 Update CLAUDE.md's lifecycle description (steps 1–8) to reflect that uploads now flow through the drama-detail page (auto-increment) or the episode-detail page (re-upload).

## 12. Spec sync

- [x] 12.1 `openspec validate admin-redesign --strict`.
- [x] 12.2 If steps 1–3d have been archived already, re-validate to ensure references resolve.
