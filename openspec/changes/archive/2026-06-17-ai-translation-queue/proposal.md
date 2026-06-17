## Why

AI translation (drama name/synopsis, tag label, actor name, subtitles) currently runs **inline in the HTTP request**, tied to the operator's browser: short text translates all target languages in one synchronous call; subtitles loop per-language from the frontend. At the upcoming scale of **40+ registered languages** this breaks down — subtitle translation alone means ~40 sequential requests holding a browser tab open for 20–40 minutes, with no resumability if the page closes, no concurrency control, and no rate-limit/backoff against the kie.ai provider (thousands of calls → 429s, runaway cost). Heavy translation must become a server-side, durable, resumable batch job.

## What Changes

- Introduce a **DB-backed async translation job queue** processed by a background worker pool inside the single uvicorn process (mirrors `app/sync.py`'s worker + `app/work_queue.py`'s pool/lock patterns).
- **Job granularity = one job per target language** (`kind` ∈ drama/tag/actor/subtitle, target ref, `source_lang` for subtitles, one `target_lang`, status `queued→running→done/failed`, attempts, error). "Translate to all languages" **fans out one job row per target language**.
- The existing AI-translate admin endpoints change from **do-the-work-synchronously** to **enqueue jobs + return 202** (no more long-held requests). **BREAKING** for the AI-translate endpoint response shape/semantics (internal `/admin/*` only; no SDK/`/api` impact).
- Background workers (`AI_TRANSLATE_CONCURRENCY`, default 1–2 to bound cost/rate) pull `queued` jobs, reuse `translate_texts` / `translate_lines` + `app/vtt.py`, write results via the existing `upsert_*_translation` / subtitle write path, call `mark_*_dirty`, and update job status; transient errors / 429 use bounded **retry with backoff**.
- **Restart reap** flips orphaned `running` jobs (mirrors `reap_orphaned_syncing`); jobs are **resumable** — re-running a fan-out only enqueues languages not already `done`. A **per-entity lock** prevents concurrent jobs writing the same episode's subtitle files.
- New **progress UI**: enqueue feedback ("已入队 N 个任务"), a jobs overview page (mirrors `/admin/sync`), and a nav badge poll (mirrors `sync-zone`) showing e.g. "字幕：12/40 语言完成".
- Translation completion feeds the **existing** dirty→sync flow unchanged (two queues in series: AI-translate, then business-sync).

## Capabilities

### New Capabilities
- `ai-translation-queue`: durable, resumable, rate-limited async execution of AI translation work — job model, fan-out/enqueue endpoints, worker pool + bounded concurrency + backoff, restart reap, per-entity locking, and operator-facing progress surfaces.

### Modified Capabilities
<!-- None. The current synchronous AI-translate implementation was never captured as its own spec; this change supersedes that implementation. The translation *write* paths (drama-meta-translations, episode-subtitles) and business-server-sync keep their existing requirements — this change only reuses them, it does not change their contracts. -->

## Impact

- **Code**: new `app/ai_jobs.py` (queue + worker + reap) and a `translation_jobs` table (additive `init_db()` migration); `app/routers/ai_translate.py` endpoints become enqueue-only; `app/main.py` lifespan starts/stops the worker pool; new admin jobs page + nav badge; reuse `app/ai_translate_client.py` + `app/vtt.py` unchanged.
- **Config**: new `AI_TRANSLATE_CONCURRENCY` env (default 1–2; fail-fast validated). Keeps `AI_TRANSLATE_API_KEY` master switch and the kie.ai `{code,msg}` error-envelope handling.
- **Constraints preserved**: single uvicorn process (queue + workers + locks in-process, state in SQLite); no SDK contract / `/api` / pipeline changes; outbound to kie.ai remains the only external hop.
- **Operator UX**: translation buttons become fire-and-forget; work survives navigation and process restart; partial failures retry per-language.
