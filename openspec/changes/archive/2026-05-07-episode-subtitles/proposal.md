## Why

Operators need to attach subtitle files to each episode — multiple per episode, one per language — and the SDK player needs URLs to render them as side-loaded subtitle tracks (no `#EXT-X-MEDIA` in the HLS playlist; out-of-band per the project's "no master playlist" constraint). This change introduces the storage and admin lifecycle for subtitles, and extends the SDK contract (`EpisodeInfo`) to expose the per-episode subtitle list. WebVTT is the only accepted format because both browser players (hls.js / native HLS) and ExoPlayer support it natively without additional muxing.

## What Changes

- Introduce a `subtitles` table keyed by `(episode_id, lang_code)`, carrying `file_url` and `uploaded_at`. FK to `episodes.episode_id` (CASCADE) and `languages.code` (RESTRICT).
- File storage convention: `OUT_DIR/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`. Served by the existing `/videos/` static mount under `/videos/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`.
- Add admin endpoints: `POST /admin/episodes/{slug}/{ep}/subtitles?lang={code}` (multipart upload, replaces if exists), `GET /admin/episodes/{slug}/{ep}/subtitles`, `DELETE /admin/episodes/{slug}/{ep}/subtitles?lang={code}`.
- Validate uploads: only `text/vtt` or `application/x-subrip` (with implicit conversion if SRT — see design); MIME + magic-byte check.
- Extend `EpisodeInfo`: add an optional `subtitles: [{langCode, label, url}] | null` field. Each entry's `label` comes from the corresponding `languages.display_label`.
- Update `episode-info-schema.json` to declare the new field as optional, nullable.
- Episode deletion (existing `episode-deletion` capability) cascades the `subtitles` rows automatically (FK CASCADE) and removes the on-disk `subtitles/` directory transitively when the episode directory is removed.

## Capabilities

### New Capabilities

- `episode-subtitles`: subtitles table + file storage convention + admin CRUD + EpisodeInfo extension.

### Modified Capabilities

- `sdk-drama-listing`: `EpisodeInfo` returned by `GET /api/episodes/{slug}/{ep}` and `GET /api/dramas/{slug}/episodes` includes the new `subtitles` array. Empty / missing → `null`. Schema-validation against the updated `episode-info-schema.json` continues to pass.

## Impact

- **Code**: `app/db.py` (new table + helpers), new router or extension in `app/routers/admin.py`, `app/routers/api.py` (`_row_to_episode_info` reads the subtitles join), `app/models.py` (Pydantic `Subtitle`/`EpisodeInfo` extension), `episode-info-schema.json` (new optional field).
- **Schema**: new `subtitles` table; new disk convention `OUT_DIR/{slug}/{ep_dir}/subtitles/`.
- **External contracts**: `EpisodeInfo` gains an optional `subtitles` array. Existing SDK clients that don't read the field continue to work.
- **No new external dependencies**.
