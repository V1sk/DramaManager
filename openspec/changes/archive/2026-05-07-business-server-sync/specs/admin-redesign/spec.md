## MODIFIED Requirements

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
