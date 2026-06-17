## 1. Schema & config

- [x] 1.1 Add `translation_jobs` table to `db.py` `init_db()` (CREATE TABLE IF NOT EXISTS): `id`, `kind`, `entity_ref`, `ep_number` (nullable), `source_lang` (nullable), `target_lang`, `status`, `attempts`, `error`, `created_at`, `updated_at`; index for queue scans; dedupe key on (`kind`,`entity_ref`,`ep_number`,`target_lang`) over non-terminal status.
- [x] 1.2 Add `AI_TRANSLATE_CONCURRENCY` to `app/config.py` (default 1–2, integer `>= 1`, fail-fast on invalid); document in `.env.example` + CLAUDE.md env table.
- [x] 1.3 Add DB helpers in `db.py`: `enqueue_translation_job(...)` (idempotent against the dedupe key), `claim_next_translation_job()` (atomic `UPDATE status='running' WHERE id=? AND status='queued'`), `mark_translation_job(id, status, error=None, attempts=...)`, `list_translation_jobs(...)`, `count_outstanding_translation_jobs()`, `reap_orphaned_translation_jobs()`.

## 2. Job worker (`app/ai_jobs.py`)

- [x] 2.1 Create `app/ai_jobs.py` with an in-memory `asyncio.Queue` seeded from DB on startup + on enqueue, and per-entity `asyncio.Lock` map (keyed by episode for subtitles), mirroring `work_queue.py`.
- [x] 2.2 Implement the per-`kind` execution: drama/tag/actor reuse `ai_translate_client.translate_texts` (single target lang); subtitle reuses `vtt` + `ai_translate_client.translate_lines` (chunked, equal-length guard); on success call the existing write path + `mark_*_dirty`.
- [x] 2.3 Implement worker loop: claim job → run under per-entity lock → persist `done`/`failed`; classify errors (429/timeout/transport → capped exponential backoff retry incrementing `attempts`; auth/credits envelope → fail with real `code/msg`).
- [x] 2.4 Implement `reap_orphaned_translation_jobs()` call at startup (flip `running`→retryable), mirroring `reap_orphaned_syncing`.
- [x] 2.5 Wire pool start/stop into `app/main.py` lifespan, gated on `settings.ai_translate_enabled`, sized by `AI_TRANSLATE_CONCURRENCY`.

## 3. Endpoints → enqueue

- [x] 3.1 Change `POST /admin/dramas/{slug}/translate`, `/admin/tags/{slug}/translate`, `/admin/actors/{slug}/translate` to fan out one job per target language (registered − source − already `done`) and return `202 {enqueued, job_ids}`.
- [x] 3.2 Change `POST /admin/episodes/{slug}/{ep}/subtitles/translate` to accept `source_lang` + target set, fan out per target language, return `202`. Add an optional `force` flag to re-enqueue `done` languages.
- [x] 3.3 Keep the `AI_TRANSLATE_API_KEY` 503 gate on all enqueue endpoints.

## 4. Operator UI

- [x] 4.1 Add `/admin/translations` jobs overview (mirror `templates/sync.html`): list `queued`/`running`/`failed` with kind/target/lang/error; re-trigger failed.
- [x] 4.2 Add `GET /admin/translations/summary` (`{enabled, outstanding_count}`) and a nav badge poll mirroring `sync-zone` (hidden when disabled / zero).
- [x] 4.3 Update drama-detail, tags, actors, and episode-detail translate controls to enqueue semantics (toast "已入队 N 个任务"); episode subtitle control shows "N/总数 完成" from job counts.

## 5. Verify & document

- [x] 5.1 Unit-test job lifecycle: enqueue dedupe, atomic claim (no double-claim), reap, resumable fan-out (skip `done`), per-language failure isolation, backoff classification — with a mocked `ai_translate_client`.
- [x] 5.2 Integration-test against a temp DB/OUT_DIR: drama + subtitle jobs run to `done`, results written, entity marked `dirty`, subtitle timestamps preserved; concurrent same-episode subtitle jobs serialized by the lock.
- [x] 5.3 Update CLAUDE.md (URL map: enqueue endpoints + `/admin/translations*`; AI-translate section: queue/worker/reap/concurrency) and the proposal's BREAKING note on the changed endpoint response shape.
