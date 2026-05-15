## Context

By the time this change starts implementation, the database has: `dramas`, `episodes`, `languages`, `translations`, `tags`, `drama_tags`, `actors`, `drama_actors`, `subtitles`. Each prior change shipped a "minimal" admin page — those pages exist but don't compose into a coherent operator workflow. Today's `templates/admin.html` is a single Jinja2 template with two forms and one table. The redesign replaces that with a multi-page surface, sharing layout via a base template, served entirely by FastAPI / Jinja2 (no client-side framework). The operator workflow is "create drama → upload episodes → add subtitles → preview in-page → eventually click sync."

## Goals / Non-Goals

**Goals:**
- A coherent navigation and page tree.
- A drama-detail page that consolidates everything about one drama (translations, tags, actors, episodes, posters).
- An episode-detail page with an in-page hls.js player so operators can review the encoded output (including subtitles) before any sync action.
- Auto-incrementing `ep_number` on new uploads; explicit `ep_number` only for re-encodes.
- Polished tag / actor / language library pages that share visual patterns.
- Designated UI slots for the step-6 sync features (status badge, "同步" buttons) so step 6 doesn't need to redo layout.

**Non-Goals:**
- No new SDK contract changes. Pure UI / admin-endpoint work.
- No actual sync logic. Slots are pre-allocated; behavior arrives in step 6.
- No client-side framework (React / Vue / etc.). Jinja2 + vanilla JS keeps the deploy story unchanged.
- No authentication / authorization. The whole service stays VPN-internal per CLAUDE.md.
- No drag-drop reordering of episodes. The natural order is `ep_number ASC`; reorder is out of scope.
- No bulk operations (bulk-upload episodes, bulk-tag dramas). Single-action operator flow only.

## Decisions

### Decision: Jinja2 multi-page over an SPA

The existing service is FastAPI + Jinja2 + a thin sprinkle of vanilla JS. Adding a build step (Vite, webpack), a JS framework (React, Vue), and a separate static-asset pipeline would dwarf the actual UI work and add ongoing maintenance to a tool that runs behind VPN with one operator at a time. A small Jinja base template + per-page templates + a single shared `static/admin.css` + per-page inline `<script>` blocks is enough.

JS stays vanilla. Where helpful, small helpers (`fetchJSON(url, opts)`, `escapeHtml(s)`, `confirmDanger(prompt)`) live in `static/admin.js` loaded by every page.

### Decision: shared base template `templates/_base.html`

Defines the `<html>` skeleton, the nav bar (`首页 / 标签 / 演员 / 语言`), a `{% block content %}` area, a flash-message strip (for redirect-with-message patterns), and `<link>` / `<script>` tags for the shared CSS / JS. Every page extends this.

The nav bar's right side has a reserved `<div id="sync-zone">` that step 6 will use for global sync status; left empty in this change.

### Decision: drama cards homepage at `GET /admin`

```
┌─────────────────────────────────────────────────────────────────┐
│ HLS 管理     [首页] [标签] [演员] [语言]                          │
├─────────────────────────────────────────────────────────────────┤
│  [+ 创建短剧]                                                    │
│                                                                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │ poster  │  │ poster  │  │ poster  │  │ poster  │            │
│  │ ──────  │  │ ──────  │  │ ──────  │  │ ──────  │            │
│  │ 琅琊榜  │  │ 步步惊心│  │ 庆余年  │  │ ...     │            │
│  │ 简介 .. │  │ 简介 .. │  │ 简介 .. │  │         │            │
│  │ 第 5 集 │  │ 第 12集 │  │ 第 8 集 │  │         │            │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘            │
└─────────────────────────────────────────────────────────────────┘
```

Server renders an HTML page with the cards already populated (no client-side render — fewer flashes). Cards are links to `/admin/dramas/{slug}`. Data source: a new helper `db.list_dramas_with_summary()` that returns `[{slug, name, synopsis_preview (first 80 chars), poster_url, ep_count, latest_ep_number, sync_status_slot}]` joining `dramas`, `translations` (default-lang name + synopsis + poster), and the existing `episodes` aggregations.

`sync_status_slot` is left empty in this change; the slot exists so step 6 fills it without re-querying.

The `[+ 创建短剧]` button is a link to `/admin/dramas/new`.

Sorting: by `latest_episode_updated_at DESC, slug ASC`. Dramas with zero episodes appear last (sorted by `dramas.created_at DESC`).

### Decision: create-drama page at `GET /admin/dramas/new`

A single form with fields:
- `slug` (text, regex hint)
- `default_lang` (select populated from `GET /api/languages` — must have at least one active language; the form errors out with a CTA to `/admin/languages` if the registry is empty)
- `name` (text, in default_lang)
- `synopsis` (textarea, optional, in default_lang)
- `poster` (image file, optional)
- `tags` (multi-select; populated from `GET /api/tags`)
- `actors` (multi-select; populated from `GET /api/actors`)

On submit, the **browser** orchestrates a sequence of existing endpoints:

```
1. POST /admin/dramas              (slug, drama_name, default_lang)        → 302/200
2. PUT  /admin/dramas/{slug}/translations/{default_lang}  {synopsis}       → 200    (only if synopsis non-empty)
3. POST /admin/dramas/{slug}/poster?lang={default_lang}   (multipart)      → 200    (only if poster file present)
4. PUT  /admin/dramas/{slug}/tags    [tag_slugs]                           → 200    (only if any selected)
5. PUT  /admin/dramas/{slug}/actors  [actor_slugs]                         → 200    (only if any selected)
6. window.location = /admin/dramas/{slug}
```

If step 1 fails (slug taken / lang invalid), the form re-renders with the error. If steps 2–5 fail, the drama is already created — the page redirects to `/admin/dramas/{slug}` with a flash message explaining the partial state, so the operator can fix it from the detail page. This is acceptable for an internal tool; rolling back via DELETE would risk losing the created drama unnecessarily.

**Alternative considered:** a single server-side endpoint that accepts everything and orchestrates atomically. Rejected because it duplicates logic from five existing endpoints and creates a parallel validation surface; the multi-step browser flow is simpler to reason about and fails gracefully.

### Decision: drama detail page layout

`GET /admin/dramas/{slug}`. Server-rendered with all data inlined (uses the new aggregate `GET /admin/dramas/{slug}/full` internally; client doesn't need to re-fetch).

```
┌──────────────────────────────────────────────────────────────────┐
│ ← 返回首页   |   [同步整部剧]  ← step 6 will populate            │
│                                                                   │
│  ┌─────────┐  琅琊榜 (zh-rCN)                                    │
│  │ poster  │  标签：[都市]  [科幻]                               │
│  │ ──────  │  演员：张三 · 李四                                  │
│  │  zh-rCN │  简介：豪门复仇 ...                                 │
│  │  ▼      │  [编辑翻译]   [编辑标签]   [编辑演员]   [删除剧]   │
│  └─────────┘                                                      │
│                                                                   │
│  集列表                                            [上传下一集]   │
│  ┌────────────────────────────────────────────┐                  │
│  │ ep | cover | status | duration | actions   │                  │
│  ├────────────────────────────────────────────┤                  │
│  │ 1 | [thumb]| ready  | 2:30     | 详情/删除 │                  │
│  │ 2 | [thumb]| encoding|—        | 详情      │                  │
│  └────────────────────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────┘
```

- The poster strip on the left has a small "▼" dropdown to switch which language's poster is displayed (default = drama's default_lang). Each option also has a small upload icon to replace that lang's poster.
- "编辑翻译" opens an inline panel listing each language with `name` / `synopsis` text inputs + save/delete buttons. Adding a new language is a "+ 添加语言" row that opens a sub-form (must include `name`).
- "编辑标签" and "编辑演员" open inline multi-selects sourced from `/api/tags` and `/api/actors`; save calls `PUT /admin/dramas/{slug}/tags` / `actors`.
- "删除剧" is **disabled** when the episodes table has any rows (with a tooltip explaining "先删除所有集"). Enabled when empty; clicking shows a `confirm()` dialog.
- "[上传下一集]" is a button that opens a file picker; on file select it POSTs to `POST /admin/dramas/{slug}/episodes` with the file, then refreshes the page.
- Episode rows: clicking "详情" navigates to `/admin/dramas/{slug}/episodes/{ep}`; clicking "删除" calls the existing `DELETE /admin/episodes/{slug}/{ep}` after `confirm()`.
- Sync-status badge for each row is reserved as `<span class="sync-badge" data-ep="{ep}"></span>` — empty in step 4, populated by step 6.

### Decision: episode detail page layout

`GET /admin/dramas/{slug}/episodes/{ep}`. Server-rendered with all subtitle/cover info inlined.

```
┌──────────────────────────────────────────────────────────────────┐
│ ← 返回 琅琊榜  |   [同步本集]   ← step 6                         │
│                                                                   │
│  ┌──────────────────────────────────────────────────┐            │
│  │            (hls.js video player)                 │            │
│  │                                                  │            │
│  │   ▶  0:00 / 2:30   [质量切换占位]  [字幕菜单]    │            │
│  └──────────────────────────────────────────────────┘            │
│                                                                   │
│  集 1  |  status: ready  |  时长: 2:30  |  分辨率: 720x1280     │
│                                                                   │
│  封面：[thumbnail]   [更换封面]                                  │
│                                                                   │
│  视频源：[原始文件名]   [重传视频] (会触发重新编码)              │
│                                                                   │
│  字幕：                                                           │
│    en      English      [文件名 / uploaded_at]   [替换] [删除]   │
│    zh-rCN  简体中文     [文件名 / uploaded_at]   [替换] [删除]   │
│    [+ 添加字幕]  (lang select + file picker)                     │
│                                                                   │
│  [删除本集]                                                       │
└──────────────────────────────────────────────────────────────────┘
```

Player initialization:

```html
<video id="player" controls></video>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script>
  const playUrl = "{{ episode.play_url }}";
  if (Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(playUrl);
    hls.attachMedia(document.getElementById('player'));
  } else if (player.canPlayType('application/vnd.apple.mpegurl')) {
    player.src = playUrl;
  }
</script>
```

Subtitle tracks are added as `<track>` elements after the player loads:

```html
<video id="player" controls crossorigin>
  {% for s in episode.subtitles %}
    <track kind="subtitles" src="{{ s.url }}" srclang="{{ s.lang_code }}" label="{{ s.label }}">
  {% endfor %}
</video>
```

The `crossorigin` attribute is required because subtitles are fetched separately; the existing `Access-Control-Allow-Origin: *` middleware covers the static mount, so this works.

For "重传视频": file picker opens; on select, the page POSTs to `POST /admin/dramas/{slug}/episodes/{ep}` with the new file. The page polls `/admin/episodes` (or a single-episode status endpoint) every 2 seconds until status leaves `encoding`, then refreshes itself.

For "替换" / "删除" subtitle: existing endpoints from `episode-subtitles`. UI confirms then refreshes the subtitle list.

For "+ 添加字幕": a small inline form with a language dropdown (sourced from `/api/languages`, **excluding** languages already in the subtitle list) and a file picker.

For "[更换封面]": existing `POST /api/episodes/{slug}/{ep}/cover`. UI confirms then refreshes.

For "[删除本集]": existing `DELETE /admin/episodes/{slug}/{ep}` after `confirm()`. On 200, redirects back to `/admin/dramas/{slug}`.

### Decision: auto-increment `ep_number` with retry on UNIQUE violation

Server-side flow for `POST /admin/dramas/{slug}/episodes`:

```
for attempt in (1, 2, 3):
    next_ep = SELECT COALESCE(MAX(ep_number), 0) + 1 FROM episodes WHERE drama_slug=?
    try:
        INSERT INTO episodes (drama_slug, ep_number, ...) VALUES (?, next_ep, ...)
        return 302 to /admin/dramas/{slug}
    except IntegrityError (UNIQUE):
        continue
return 503 "concurrent uploads collided; retry"
```

In practice the operator pool is single-digit; one retry is plenty. The 503 path exists to fail loudly rather than silently skipping a number.

The handler still validates the upload (`ffprobe`, cover extraction) before the insert, same as today's `/admin/upload` — failed ffprobe → 400 + temp file removed.

### Decision: new endpoints `POST /admin/dramas/{slug}/episodes` and `POST /admin/dramas/{slug}/episodes/{ep}`

| Method | Path | Body | Behavior |
|---|---|---|---|
| `POST` | `/admin/dramas/{slug}/episodes` | multipart `video` | Auto-increment ep_number; create new row + enqueue pipeline. 404 if drama missing. 503 if 3 retries collide. |
| `POST` | `/admin/dramas/{slug}/episodes/{ep}` | multipart `video` | Re-encode existing episode. 404 if episode missing. 409 if `status=encoding`. |

The legacy `POST /admin/upload` is removed. CLAUDE.md's "Direct pipeline commands" reproduction recipes still work (they invoke `pipeline.sh` directly, not the HTTP layer).

### Decision: aggregate read endpoint `GET /admin/dramas/{slug}/full`

Returns a single JSON shape consolidating drama-detail-page data:

```json
{
  "slug": "ly",
  "default_lang": "zh-rCN",
  "created_at": "...",
  "updated_at": "...",
  "translations": {
    "zh-rCN": {"name": "琅琊榜", "synopsis": "...", "poster": "/videos/ly/poster/zh-rCN.jpg"},
    "en":     {"name": "Langya Bang", "synopsis": null, "poster": null}
  },
  "tags":   [{"slug": "urban", "label": "都市"}, ...],
  "actors": [{"slug": "zhang-san", "name": "张三"}, ...],
  "episodes": [
    {"ep_number": 1, "episode_id": "ly-ep-1", "status": "ready", "duration_ms": 150000, "cover_url": "...", "play_url": "...", "subtitle_count": 2, "updated_at": "..."},
    ...
  ]
}
```

This lets the drama-detail server-rendered template fill everything without N database round-trips.

### Decision: hls.js loaded from public CDN (jsDelivr), with vendoring fallback

`<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5/dist/hls.min.js">` is the default. If the operator network blocks public CDN (some VPNs do), the operator can drop the file under `app/static/vendor/hls.min.js` and uncomment a local include in `_base.html` (commented-out by default). Documented in CLAUDE.md.

This is consistent with the project's "internal-network only" posture: an attacker on the network couldn't tamper with hls.js delivery any more than they could tamper with the unauthenticated admin endpoints.

### Decision: shared CSS and JS in `app/static/`

New layout:
```
app/static/
  admin.css          # all admin-page styles
  admin.js           # fetchJSON / escapeHtml / confirmDanger helpers
  vendor/            # optional vendored hls.min.js (gitignored or committed; TBD)
```

`app/main.py` already mounts `/videos`. We add `app.mount("/static", StaticFiles(directory="app/static"))` for these assets. The `/static` mount is read-only.

## Risks / Trade-offs

- **Risk: in-page hls.js DRM behavior diverges from production SDK.** hls.js fetches the AES-128 key via `XMLHttpRequest` against `#EXT-X-KEY:URI`; the server-side player UX should match what ExoPlayer does, but minor differences exist (caching, range requests). → Documented as "preview only; final verification still happens via SDK build" in CLAUDE.md.
- **Risk: multi-step create-drama flow is non-atomic.** A failure mid-flow leaves a partially-populated drama. → Mitigation: the redirect-with-flash UX guides the operator to fix the partial state from the drama detail page. Acceptable for an admin tool.
- **Risk: hls.js CDN dependency.** A CDN outage breaks the in-page player but not the rest of the admin. → Vendoring fallback documented.
- **Trade-off: server-side rendering for drama-detail makes some interactions require full page reloads.** Counter: pages are small, reloads are fast on internal LAN, and avoiding a client framework saves more time than it costs.
- **Risk: `[上传下一集]` and `[重传视频]` both go through the pipeline queue (one job at a time).** Operators uploading multiple episodes in succession will queue up. The drama detail page must show "正在编码" status clearly. → The episodes table polls `/admin/episodes` every 5 seconds (existing behavior) and updates status badges. Add a queue-depth indicator if it becomes annoying.
- **Trade-off: removing `POST /admin/upload` is a hard break for any external scripts pointing at it.** → Acceptable because no production exists. If anyone has scripts they need to redirect them to `POST /admin/dramas/{slug}/episodes/{ep}`.

## Migration Plan

This is a UI-layer change with one BREAKING endpoint removal. Steps:

1. Stop server.
2. Deploy new code; existing DB is unchanged. (All schema work was done in steps 1–3d.)
3. Operators bookmark the new URLs (`/admin` still redirects from `/`; the page just looks different).
4. Update CLAUDE.md upload examples to reference the new endpoints.

Rollback: revert the code; DB unchanged.

## Open Questions

- Should `GET /admin/dramas/{slug}/full` include the **list of available_langs** that a drama has translations in (handy for the translation-editor UI)? Leaning **yes**; trivial to compute.
- Should the homepage card show drama-level sync status before step 6 ships? **No** — the slot is reserved but empty. Showing "未同步" everywhere when there's no sync system yet is misleading.
- Should the episode detail page support **previewing the 540p / 1080p fallback playlists** in addition to the default 720p? Probably nice-to-have; can add a small "切换码率" dropdown that swaps `playUrl`. Listed as an optional task.
