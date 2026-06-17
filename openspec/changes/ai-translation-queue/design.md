## Context

AI translation was added outside the spec flow and runs **inline in the request**: `app/routers/ai_translate.py` calls `app/ai_translate_client.py` (and `app/vtt.py` for subtitles) synchronously, writing via `db.upsert_*_translation` / the subtitle write path and `mark_*_dirty`. Short text translates all target languages in one call; subtitles loop **per language from the browser**. The codebase already runs two in-process queues under a **single uvicorn process** (a hard constraint): `app/work_queue.py` (asyncio worker pool + per-episode `asyncio.Lock`, `PIPELINE_CONCURRENCY`) for encoding, and `app/sync.py` (single FIFO worker + DB `sync_status` state machine + `reap_orphaned_syncing` on startup) for business-server sync. This change adds a third, structurally identical queue for AI translation. At 40+ languages the inline model produces 40 sequential browser-bound requests (20–40 min, non-resumable) and uncontrolled provider load.

## Goals / Non-Goals

**Goals:**
- Make heavy translation durable, resumable, and decoupled from the HTTP request / browser lifecycle.
- Bound provider concurrency, cost, and rate (backoff on 429) from one place.
- Reuse existing primitives unchanged: `ai_translate_client`, `vtt`, the translation/subtitle write paths, and the downstream dirty→business-sync flow.
- Give operators fire-and-forget UX with progress visibility, matching the existing `/admin/sync` + `sync-zone` patterns.

**Non-Goals:**
- No SDK / `/api` / `/drm` / pipeline changes; no master-playlist or DRM impact.
- No multi-process / external broker (Celery/Redis) — the single-process + SQLite constraint stays.
- No automatic retranslation on source change; enqueue is operator-triggered.
- No origin/provenance tracking on translations (prior product decision: overwrite, no `origin` column).
- Not changing the translation *quality* prompts or the per-chunk subtitle algorithm.

## Decisions

### Job granularity: one row per (target ref, target_lang)
Chosen over a single "all languages" batch job. Rationale: per-language status gives natural progress ("12/40"), per-language retry of only failures, and per-language parallelism up to the cap. Cost is more rows (40 per fan-out), which SQLite handles trivially. Alternative (one job looping internally) was rejected: coarse progress, all-or-nothing retry, and it would re-implement queue semantics inside a job.

### Storage: a `translation_jobs` table, added via the existing additive migration
A dedicated table rather than overloading `sync_status`. Columns: `id` (pk), `kind`, `entity_ref` (slug or episode id), `ep_number` (nullable, subtitles), `source_lang` (nullable), `target_lang`, `status`, `attempts`, `error`, `created_at`, `updated_at`. A partial uniqueness/dedupe key on (`kind`, `entity_ref`, `ep_number`, `target_lang`, non-terminal status) prevents duplicate in-flight jobs for the same unit. Added by `init_db()`'s `_migrate_add_columns`-style path (CREATE TABLE IF NOT EXISTS) — no `hls.db` reset.

### Worker model: mirror `sync.py` + `work_queue.py`
A pool of `AI_TRANSLATE_CONCURRENCY` worker coroutines started in `app/main.py` lifespan (alongside the existing pools), each: claim a `queued` job (atomic `UPDATE ... SET status='running'` guarded so two workers can't claim the same row), run it, persist `done`/`failed`. An in-memory `asyncio.Queue` is seeded from the DB on startup and on each enqueue; the DB is the source of truth so restarts are safe. Per-entity `asyncio.Lock` (keyed by episode for subtitles) serializes same-entity writes, exactly like `work_queue.py`'s `_episode_locks`.

### Endpoints become enqueue-only (BREAKING, internal)
`POST /admin/{dramas,tags,actors}/{slug}/translate` and `POST /admin/episodes/{slug}/{ep}/subtitles/translate` insert jobs and return `202 {enqueued, job_ids}` instead of doing the work. The fan-out computes target languages = registered languages − source, minus those already `done` (resumability). Subtitle source defaults to an existing subtitle language (operator-chosen as today).

### Retry/backoff and error semantics
Reuse the kie.ai `{code,msg}` envelope detection already in `ai_translate_client._chat`. Classify: 429/timeout/transport → retry with capped exponential backoff (bounded `attempts`); other envelopes (auth/credits) → fail immediately with the real `code/msg` in `error`. The subtitle equal-length guard stays a hard failure (never write a misaligned VTT).

### UI: reuse the sync surfaces
A `/admin/translations` (jobs) overview mirroring `/admin/sync`; a `GET /admin/translations/summary` polled by a nav badge mirroring `sync-zone`; entity pages show enqueue toasts and (for subtitles) "N/40 完成" derived from job counts. Failed jobs are re-triggerable from the overview.

## Risks / Trade-offs

- **[Provider cost/rate blow-up at 40 langs × many episodes]** → Hard `AI_TRANSLATE_CONCURRENCY` cap (default low), backoff on 429, and dedupe key to avoid duplicate in-flight jobs; surface `credits_consumed` from responses in logs for cost visibility.
- **[Two workers claiming the same job]** → Atomic conditional `UPDATE status='running' WHERE id=? AND status='queued'`; only the worker whose update affected a row owns the job.
- **[Stale source: subtitle/source edited after enqueue]** → Job reads the source at run time, not enqueue time; last-writer-wins, consistent with the existing overwrite policy. Documented, not prevented.
- **[Resumability vs. forced re-translation]** → "Skip already-`done`" is the default; provide an explicit "force re-translate" that enqueues regardless. Avoids surprising no-ops while keeping cheap re-runs.
- **[Long subtitle jobs still long, just off the request path]** → Acceptable; durability + progress + cancel/retry replace the browser wait. Per-job timeout (`AI_TRANSLATE_TIMEOUT`) still bounds individual provider calls.
- **[Orphaned `running` on crash]** → Startup reap (mirrors `reap_orphaned_syncing`) makes them retryable; idempotent writes (upsert / file overwrite) make re-runs safe.

## Migration Plan

1. Additive `translation_jobs` table via `init_db()` (no data migration, no `hls.db` reset).
2. Add `AI_TRANSLATE_CONCURRENCY` (fail-fast validated; default 1–2) and document in `.env.example` / CLAUDE.md.
3. Land `app/ai_jobs.py`; wire worker pool + reap into lifespan; switch the four translate endpoints to enqueue.
4. Update the frontend translate buttons to enqueue semantics; add the jobs overview + nav badge.
5. Rollback: revert the endpoints to synchronous (previous behavior) and stop starting the worker pool; the unused `translation_jobs` table is inert.

## Open Questions

- Retry caps: concrete `max_attempts` and backoff schedule (e.g. 3 attempts, 2s→8s→32s) — tune against kie.ai's actual 429 behavior.
- Should the nav badge / overview be gated behind a permission (like `can_sync`), or visible to any logged-in operator (like translation editing today)? Leaning toward the latter for parity with manual translation.
- Job retention/GC: keep `done` rows for history, or prune after N days? Start by keeping them; revisit if the table grows.
