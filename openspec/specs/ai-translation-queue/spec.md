# ai-translation-queue Specification

## Purpose
TBD - created by archiving change ai-translation-queue. Update Purpose after archive.
## Requirements
### Requirement: Per-target-language translation jobs

The system SHALL model AI translation work as durable jobs persisted in SQLite, one job per (target reference, target language). A job SHALL carry: `kind` (one of `drama`, `tag`, `actor`, `subtitle`), the target reference (drama slug; tag/actor slug; or episode id + ep number for subtitles), `source_lang` (required for `subtitle`; the entity's `default_lang` otherwise), exactly one `target_lang`, a `status` (`queued`, `running`, `done`, `failed`), an `attempts` count, and a last `error` message.

#### Scenario: Enqueue translate-to-all fans out one job per language
- **WHEN** an operator triggers "translate to all languages" for an entity with N registered languages other than the source
- **THEN** the system inserts N `queued` job rows (one per target language) and returns `202` without performing any translation in the request

#### Scenario: Job row records its unit of work
- **WHEN** a job is created for a subtitle translation
- **THEN** the row records `kind=subtitle`, the episode reference, the chosen `source_lang`, exactly one `target_lang`, `status=queued`, and `attempts=0`

### Requirement: Enqueue endpoints replace synchronous translation

The AI-translate admin endpoints (drama, tag, actor, subtitle) SHALL enqueue jobs and return promptly (`202`) instead of translating inline within the request. They SHALL NOT hold the HTTP request open for the duration of provider calls.

#### Scenario: Endpoint returns immediately after enqueue
- **WHEN** an operator posts an AI-translate request
- **THEN** the endpoint returns `202` with the count/ids of enqueued jobs, and the response time does not depend on provider latency

#### Scenario: Disabled when provider unconfigured
- **WHEN** `AI_TRANSLATE_API_KEY` is unset
- **THEN** the enqueue endpoints return `503` and the translation UI controls are hidden

### Requirement: Background worker pool with bounded concurrency

The system SHALL process `queued` jobs with a pool of background worker coroutines started in the application lifespan, capped by `AI_TRANSLATE_CONCURRENCY` (default small, e.g. 1–2) to bound provider rate and cost. Workers SHALL reuse the existing translation client (`translate_texts` / `translate_lines`) and VTT helpers, write results through the existing translation/subtitle write paths, and mark the affected entity dirty on success.

#### Scenario: Concurrency cap is honored
- **WHEN** more `queued` jobs exist than `AI_TRANSLATE_CONCURRENCY`
- **THEN** at most `AI_TRANSLATE_CONCURRENCY` jobs are in `running` state at any time and the rest wait in `queued`

#### Scenario: Successful job writes result and marks dirty
- **WHEN** a worker completes a drama-translation job for `target_lang`
- **THEN** the translation is upserted for that language and the drama is marked `sync_status=dirty` so the existing business-sync flow can later push it

#### Scenario: Invalid concurrency fails fast
- **WHEN** `AI_TRANSLATE_CONCURRENCY` is set to a non-integer or value `< 1`
- **THEN** startup fails fast with a clear error

### Requirement: Retry with backoff on transient and rate-limit errors

A worker SHALL retry a job a bounded number of times with backoff when the provider returns a transient error or rate-limit (e.g. HTTP/`code` 429), incrementing `attempts`. After exhausting retries the job SHALL be marked `failed` with the provider's error surfaced in `error`.

#### Scenario: Rate-limit triggers backoff retry
- **WHEN** a provider call returns a 429 / rate-limit envelope
- **THEN** the worker waits with backoff and retries up to the bounded limit before failing the job

#### Scenario: Permanent error surfaces real reason
- **WHEN** the provider returns a non-retryable error envelope (e.g. insufficient credits)
- **THEN** the job is marked `failed` and `error` contains the provider's `code`/`msg`, not an opaque message

### Requirement: Overwrite-by-design fan-out, per-language failure isolation

One-click "translate to all languages" SHALL (re)enqueue EVERY target language (every registered language except the source), overwriting existing translations. It SHALL NOT skip languages based on prior job history (`done` records), because that history goes stale the moment a translation is edited or deleted out of band and would silently drop languages that still need (re)translating. A failure in one language's job SHALL NOT affect other languages' jobs. Genuinely in-flight (`queued`/`running`) duplicates SHALL still be suppressed (reported as skipped), so double-clicking does not double-enqueue.

#### Scenario: Re-run after a manual deletion re-translates everything
- **WHEN** an operator deletes some languages' translations and then re-triggers translate-to-all
- **THEN** ALL target languages are enqueued (not a no-op), regardless of prior `done` records, and existing translations are overwritten

#### Scenario: Double-click does not double-enqueue
- **WHEN** an operator triggers translate-to-all twice before the first batch finishes
- **THEN** the second trigger enqueues nothing new; the still-in-flight languages are reported as skipped

#### Scenario: One language failing does not block others
- **WHEN** one target language's job fails
- **THEN** the remaining target languages' jobs still run and complete independently

### Requirement: Restart reaping of orphaned jobs

On startup the system SHALL reap jobs left in `running` (orphaned by a process restart), flipping them to `failed` (or re-queuing) so they can be retried, mirroring the existing sync-worker reap.

#### Scenario: Orphaned running job is reaped
- **WHEN** the process restarts while a job is `running`
- **THEN** at startup that job is reaped out of `running` and made retryable, never left stuck

### Requirement: Per-entity locking for write safety

The system SHALL serialize jobs that write the same entity's files or rows (notably an episode's subtitle directory) with a per-entity lock, so concurrent jobs never corrupt the same `ep-{n}/subtitles/` files. Jobs for different entities MAY run concurrently up to the concurrency cap.

#### Scenario: Concurrent subtitle jobs for one episode are serialized
- **WHEN** two jobs translate subtitles for the same episode at the same time
- **THEN** they execute one at a time under the per-episode lock, while jobs for other episodes proceed in parallel

### Requirement: Operator-facing progress surfaces

The system SHALL provide operator visibility into translation jobs: enqueue feedback on the triggering page, a jobs overview listing non-terminal and recently-failed jobs (mirroring the sync overview), and a navigation badge that polls a summary endpoint. Per-entity progress SHALL be expressible (e.g. "字幕：12/40 语言完成").

#### Scenario: Jobs overview lists in-flight and failed jobs
- **WHEN** an operator opens the translation jobs overview
- **THEN** it lists `queued`/`running`/`failed` jobs with their kind, target, language, and error (for failures), and can re-trigger failed ones

#### Scenario: Nav badge reflects outstanding work
- **WHEN** there are outstanding (non-`done`) translation jobs
- **THEN** a nav badge shows the count and links to the jobs overview; when none remain and the feature is enabled, no badge is shown

### Requirement: Translation queue feeds the existing sync flow unchanged

Completed translation jobs SHALL only write to the staging editor state (translations rows / local + staging subtitle files) and mark the entity dirty. They SHALL NOT push to the business server directly; the existing business-sync queue remains the sole path to prod.

#### Scenario: Completion does not bypass sync
- **WHEN** a translation job completes successfully
- **THEN** the entity becomes `dirty` and prod is updated only later, by the existing business-sync worker when the operator syncs

