## Context

Subtitles for short-drama HLS streams need to:
1. Be uploaded by operators per episode, one file per language.
2. Be served by URL to the SDK / player.
3. NOT be embedded in the HLS playlist (the project's "no master playlist / no ABR" constraint excludes `#EXT-X-MEDIA`-style subtitle tracks).

The SDK consumes the subtitle URL list via `EpisodeInfo` and side-loads them into ExoPlayer's MergingMediaSource (or hls.js's text-track API) at player attach time. This change owns the storage, admin lifecycle, and SDK contract extension. No pipeline / encoding work is involved — subtitle files are static assets uploaded directly to disk.

## Goals / Non-Goals

**Goals:**
- A `subtitles` table joining episodes ↔ languages with a file URL.
- Admin endpoints to upload, list, and remove subtitles per episode-language.
- WebVTT-only validation at upload time.
- `EpisodeInfo.subtitles` field exposed to the SDK with `{langCode, label, url}` for each present language.
- Schema update to `episode-info-schema.json` reflecting the new optional field.

**Non-Goals:**
- No SRT-to-VTT conversion. Operators must upload `.vtt`. Adding conversion is a future change if needed.
- No `#EXT-X-MEDIA` injection into the HLS playlist. Subtitle delivery stays out-of-band.
- No per-subtitle custom labels (e.g. `"中文(简)"` vs `"中文(繁)"`). The label comes from `languages.display_label`. Custom per-episode-subtitle labels can be added later via a `translations` row keyed by a synthetic `entity_type`.
- No locale negotiation on subtitle list — the API returns all available subtitles for the episode; the client picks. Step 6's `?lang=` work doesn't apply here.
- No streaming / chunked subtitle (CMAF text track). VTT static file only.

## Decisions

### Decision: composite PK `(episode_id, lang_code)` rather than autoincrement id

Each `(episode, language)` pair has at most one subtitle. The composite PK enforces this naturally. UPSERT semantics on the same composite key handle re-uploads cleanly.

### Decision: FK on `episodes.episode_id` (the UNIQUE TEXT column), not `episodes.id` (autoinc INTEGER)

`episode_id` is the SDK contract key (`{drama_slug}-ep-{n}`), used in URLs and external systems. Using it as the FK keeps the foreign-key target stable and human-readable in DB inspections. SQLite supports FKs onto UNIQUE non-PK columns.

**Alternative considered:** FK onto `episodes.id`. Rejected because most readers / writers in this codebase work in terms of `episode_id`; an integer FK would require an extra lookup at every join.

### Decision: file storage `OUT_DIR/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`

Sibling to the `720p/`, `540p/`, `1080p/` directories that the pipeline produces. The `subtitles/` subdirectory is created lazily on first upload. The static mount at `/videos/` covers it transparently. Episode deletion's existing `shutil.rmtree(ep_dir_path)` removes `subtitles/` along with everything else — no extra cleanup code.

### Decision: WebVTT-only at upload, MIME + magic-byte check

Accepted MIME types: `text/vtt`, `text/plain` (browsers sometimes mis-label). Reject `.srt`, `.ass`, etc. for now. Beyond MIME, the handler reads the first 6 bytes of the file and verifies they start with `WEBVTT` (the format's magic header). Failures → 400.

**Trade-off:** no SRT support means operators must convert externally. → Acceptable for v1; an "auto-convert if SRT" pass can be added later (would require the `webvtt-py` or `pysubs2` dependency).

### Decision: subtitle URLs are host-relative

`/videos/{slug}/{ep_dir}/subtitles/{lang_code}.vtt`. Same convention as `playUrl`, `coverUrl`, `keyUri`. When OSS staging/prod separation lands (step 5), subtitles are uploaded to OSS like other static assets, and these URLs are rewritten to point at OSS — exactly mirroring the existing init/segment rewrite logic. Stays consistent.

### Decision: re-upload overwrites; no version history

`POST /admin/episodes/{slug}/{ep}/subtitles?lang=en` with an existing entry: the file is overwritten on disk and the `uploaded_at` is refreshed. No prior versions are kept. Short-drama subtitles are typically replaced for fixes, not annotated; versioning would add complexity for no clear value.

### Decision: HTTP layer

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/episodes/{slug}/{ep}/subtitles?lang={code}` | Multipart `file`. Upserts. 400 on bad MIME/magic. 404 if episode/lang missing. |
| `GET` | `/admin/episodes/{slug}/{ep}/subtitles` | `[{lang_code, label, url, uploaded_at}, ...]` ordered by `lang_code`. 404 if episode missing. |
| `DELETE` | `/admin/episodes/{slug}/{ep}/subtitles?lang={code}` | Removes file + row. 204 / 404. |

### Decision: `EpisodeInfo.subtitles` is `null` when no subtitles exist (not empty array)

Matches the existing pattern: `coverUrl: null`, `drm: null`, `fallback: null` for absent. An empty array `[]` would mean "subtitles intentionally none" — but `null` carries the same meaning and is the established convention in this schema. Compromise: when at least one subtitle exists, return an array of objects (no nulls inside); otherwise return `null`.

The `episode-info-schema.json` update declares the field as `array | null` with `additionalProperties: false` on each subtitle object.

### Decision: subtitle label uses `languages.display_label` (admin label)

The label shown to end users (in the SDK's subtitle picker) is the admin-configured `display_label` for the language. Examples: `简体中文`, `English`, `日本語`. This matches the operator's mental model — same label appears in admin and SDK.

When a per-subtitle custom label is needed in the future (e.g. `中文(简)` vs `中文(繁)` for a single language), it can be stored in `translations` under `entity_type='episode_subtitle'`, `entity_id='{slug}-ep-{n}/{lang_code}'`, `field='label'`. Not required now.

## Risks / Trade-offs

- **Risk: WebVTT files contain timing data that must align with the encoded video.** If the operator uploads a subtitle from a different cut, it will be misaligned. → Outside the system's purview. Consider showing a small "preview" link in admin so the operator can check sync via the in-page hls.js player (added in step 4 / `admin-redesign`).
- **Risk: subtitle UTF-8 BOM might break some players.** → Validation strips the BOM if present at the start of the file (after the WEBVTT magic check, transparently). Documented in tasks.
- **Trade-off: no SRT auto-convert.** Operators have to convert externally. → Acceptable; can add later.

## Migration Plan

No schema breakage outside the new `subtitles` table. The `EpisodeInfo` JSON gains an optional field; existing SDK clients ignore unknown fields per JSON spec; new clients read it. No data migration needed.

## Open Questions

- Should the subtitle endpoint expose its own `Cache-Control` headers? VTT files are immutable per upload (we overwrite, but URL stays the same), so `max-age=0, must-revalidate` is safest. Static mount default headers are probably fine. Defer.
- Should we support uploading multiple subtitle files for the same language as a "set" (e.g. both standard and "for hearing impaired" versions)? Out of scope; one file per (episode, language).
