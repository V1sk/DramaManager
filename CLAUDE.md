# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Internal HLS management server for the short-drama `media3-shortdrama` Android SDK. It wraps the existing shell pipeline (`pipeline.sh` + stage scripts) behind a FastAPI service: operators upload a source MP4 via the `/admin` web page, the server runs the 3-rung CMAF ladder (540p / 720p / 1080p) + AES-128-CBC encryption, and exposes SDK-ready `EpisodeInfo` JSON plus static hosting of the playlists/segments/covers. `episode-info-schema.json` is the authoritative schema for the SDK contract.

## Management server

Run:

```bash
# one-time
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# start
./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

URL 归属（HLS 自家 SDK 端点，本地审片预览用）：

- **永远是业务 host 相对路径** (`/videos/...`, `/drm/...`): `videoTracks[].url` / `coverUrl` / `drm.keyUri` / `DramaSummary.posterUrl` 全部如此。m3u8 里的 `#EXT-X-MAP:URI` 与 segment 行也是相对文件名（`init-720p.mp4` / `seg-720p-0.m4s`），播放器按 m3u8 自身 host 通过 `/videos/` 静态挂载读本地切片。
- **不绕桶**：HLS 服务器只负责本地审片，预览不需要 CDN，所以不管 `STORAGE_PROVIDER` 选 `oss` / `tos` / `none`，对外 API 形态都一致。桶上传只为给业务服务器同步资产用，预览页直接读 `OUT_DIR` 下的本地文件，零 CORS、零公网出站。
- **生产侧的 CDN-友好 URL**：业务服务器收到 sync payload 后拿到的是 path-only 形态（`Drama/prod/...`），它持有自己的 `MEDIA_BASE_URL`（默认 TOS 直链，可换 CDN 域名）给客户端拼前缀。详见 `docs/business-server-sync-api.md`。

`PUBLIC_BASE_URL` 已废弃；客户端从自己发起请求的 host 拼相对路径即可。

Environment variables:

| var | required | default | purpose |
|---|---|---|---|
| `OUT_DIR` | no | `./out` | Pipeline artifact root (`<OUT_DIR>/<drama_slug>/...`). |
| `DB_PATH` | no | `./hls.db` | SQLite file. |
| `UPLOAD_TMP_DIR` | no | `./tmp` | Staging dir for uploaded sources; files are deleted after the worker finishes the job. |
| `STORAGE_PROVIDER` | no | `none` | 选 bucket 厂商：`none` / `oss` (Aliyun OSS) / `tos` (Volcengine TOS)。设为 `oss` 或 `tos` 时启用上传：worker 把每档 init.mp4 + 加密 .m4s 推到所选桶的 **staging 前缀** (`Drama/staging/...`)，并把本地 m3u8 里 init / segment 引用改写成绝对 staging URL。`#EXT-X-KEY:URI` 不动。endpoint / bucket / 前缀硬编码在 `app/storage/oss_provider.py` 或 `app/storage/tos_provider.py`，不走 env；AK/SK 走 gitignore 的 `app/storage/credentials.py`（fresh clone 后按 `credentials.example.py` 拷一份填自己的密钥，按人分配，不进 git）。Prod 前缀 (`Drama/prod/...`) 只在执行业务服务器同步时由 `publish_ladder_to_prod` server-side copy 进去（详见 "业务服务器同步"）。非法值 → 启动期 fail-fast。 |
| `OSS_ENABLED` | no | `false` | **Deprecated alias** — `OSS_ENABLED=true` 等价于 `STORAGE_PROVIDER=oss`。仅在 `STORAGE_PROVIDER` 未设时生效；新部署请直接用 `STORAGE_PROVIDER`。 |
| `DEFAULT_LADDER` | no | `720p` | 控制 **admin 审片预览**默认用哪档 ladder rung（重写 admin 侧的 `play_url`，见 `db._apply_default_ladder`）。可选 `540p` / `720p` / `1080p`。**在读取时动态生效**——改 env 重启 uvicorn 就能切换，不需要重新编码（pipeline 始终生产三档完整切片）。SDK 的 `GET /api/*` 不受影响：`EpisodeInfo.videoTracks` 永远带全三档，由客户端选档。非法值在启动时 fail-fast。 |
| `BUSINESS_SYNC_BASE_URL` | no | _unset_ | 业务服务器同步 base URL（`https://...`，无尾部 `/`）。**未设时同步功能整体禁用**：`POST /admin/dramas/{slug}/sync` 等返回 503，导航栏 `sync-zone` 不显示。设了就必须同时设 `BUSINESS_SYNC_API_KEY`。 |
| `BUSINESS_SYNC_API_KEY` | required iff base URL set | _unset_ | 业务服务器握手用的共享密钥，作为 `X-API-Key` 头随每个 `/sync/*` 请求发送。`BUSINESS_SYNC_BASE_URL` 设而本变量未设 → 启动期 fail-fast。 |
| `BUSINESS_SYNC_TIMEOUT` | no | `30` | 单次 `/sync/*` HTTP 请求的超时（秒）。整数；非正值 → 启动期 fail-fast。 |
| `PIPELINE_CONCURRENCY` | no | `2` | 并发跑的 pipeline job 数（encode + encrypt + bucket publish）。每个 job 是独立 `pipeline.sh` 子进程；ffmpeg 本身已多线程，调高会过度订阅 CPU，有空闲核才往上加。同一集的多个 job 仍由 `work_queue.py` 的 per-episode 锁串行化，绝不并行写同一个 `ep-{n}/` 目录。整数 `>= 1`；非法值 → 启动期 fail-fast。 |
| `SESSION_SECRET_KEY` | **yes** | _unset_ | 签名 `/admin` 后台会话 cookie 的密钥（`admin-accounts-auth`）。设为一串长随机字符串；**未设 → 启动期 fail-fast**。值要在重启间保持稳定，否则每次重启会让所有操作员掉登录。 |
| `ADMIN_INITIAL_PASSWORD` | first boot only | _unset_ | 首次启动引导用：`users` 表为空时 `init_db()` 用它创建 `admin` 账号（`must_change_pw=1`，首次登录强制改密）。`users` 已有行时本变量被忽略。`users` 为空却未设本变量 → 启动期 fail-fast。 |

Drama / language lifecycle (introduced by `drama-as-entity` + `i18n-foundation`):

```
POST /admin/languages  →  language row (e.g. zh-rCN) exists
       ↓
POST /admin/dramas     →  drama row exists; FK on default_lang → languages.code
       ↓
POST /admin/upload     →  episode pipeline → status=ready

DELETE /admin/episodes/{slug}/{ep}  →  episode row + ep dir + keys/* removed; drama row stays
DELETE /admin/dramas/{slug}         →  only allowed when 0 episodes; drama row + OUT_DIR/{slug}/ removed
DELETE /admin/languages/{code}      →  only allowed when no drama or translation references the code (FK + pre-check guards both ways)
```

Tables: `languages` (code PK + display_label), `dramas` (slug PK + name + default_lang FK to languages), `translations` (entity_type, entity_id, lang_code FK to languages, field, value — the generic store for tag / actor / drama / subtitle translations once steps 3a–3d land), `episodes` (FK to dramas; same DRM / playlist columns as before).

There is no "active / inactive" state on languages or accounts — to remove either, delete the row. Languages are protected by FK (delete returns 409 if any drama / translation / subtitle still references the code; operator must clean up references first). The first thing operators must do on a fresh deploy is seed at least one language via `/admin/languages`.

`init_db()` runs a one-shot self-test on startup: it tries an INSERT into `translations` with a non-existent `lang_code`; the FK MUST raise `IntegrityError`. If it doesn't, startup fails fast — the whole i18n / drama integrity story collapses without enforced FKs.

URL map:

所有 `/admin/*` 路由都要求登录会话（`admin-accounts-auth`）；`/api/*`、`/drm/*`、`/videos/*` 保持开放（SDK 契约，靠 VPN）。下表 `[gate]` 标注额外的权限要求。

| route | purpose |
|---|---|
| `GET /` | 302 → `/admin`（未登录则 `/admin` 再 303 → `/login`） |
| `GET /login` | 登录页 HTML（无鉴权）。已登录则 303 → `/admin` |
| `POST /login` | form: `username`, `password`, `next`. 成功 303 → `next`；失败重渲染登录页 (401)。带登录节流：连续 5 次失败锁 5 分钟 |
| `POST /logout` | 清会话，303 → `/login` |
| `GET /admin/accounts` | HTML：账号管理页（建号 + 列表 + 每行角色/状态/权限/重置密码/删除）。`[gate]` admin |
| `POST /admin/accounts` | form: `username`, `password`, `role`, `can_delete`, `can_sync`. 新号 `must_change_pw=1`。303 / 400 / 409 (重名)。`[gate]` admin |
| `PATCH /admin/accounts/{username}` | JSON：可选 `role` / `can_delete` / `can_sync`。200 / 400 / 404 / 409 (最后一个 admin 不能降级)。`[gate]` admin |
| `POST /admin/accounts/{username}/password` | JSON `{password}`：管理员重置密码，置 `must_change_pw=1`。200 / 404。`[gate]` admin |
| `DELETE /admin/accounts/{username}` | 204 / 404 / 409 (最后一个 admin 不能删)。`[gate]` admin |
| `GET/POST /admin/account/password` | 自助改密页 + 提交（任意已登录账号）。提交校验当前密码，成功清 `must_change_pw` 并 303 → `/admin` |
| `GET /admin/audit` | HTML：操作记录页，`?page=N` 分页（每页 50，最新在前）。`[gate]` admin |
| `GET /admin` | management HTML: drama-create form (with default_lang dropdown sourced from `/api/languages`) + episode-upload form + episode list (5 s auto-refresh) |
| `GET /admin/languages` | HTML: language registry (create form + table; toggle / delete per row) |
| `GET /admin/languages.json` | JSON list of all languages; fields `code, display_label, created_at, updated_at` |
| `POST /admin/languages` | form: `code`, `display_label`. 302 / 409 (code taken) / 400 (validation). |
| `PATCH /admin/languages/{code}` | JSON body with optional `display_label`. `code` itself is immutable. 200 / 400 / 404. |
| `DELETE /admin/languages/{code}` | 204 / 404 / 409 with `{"error": ..., "dramas": n, "translations": m}` if referenced. `[gate]` can_delete |
| `GET /admin/dramas/new` | HTML: drama-create form (slug + default_lang + name + synopsis + poster + tags multi-select + actors multi-select). Browser orchestrates 5 endpoints in sequence. |
| `GET /admin/dramas/{slug}` | HTML: drama detail page (translations editor, tags / actors editors, episodes table with auto-increment upload, embedded poster strip). |
| `GET /admin/dramas/{slug}/full` | JSON: aggregate read — drama row + per-language translations + tags + actors + episodes. Used by server render and client refresh. |
| `GET /admin/dramas/{slug}/episodes/{ep}` | HTML: episode detail page with embedded hls.js player, subtitle list, cover / video re-upload, delete. |
| `POST /admin/dramas` | multipart: `drama_slug`, `drama_name`, `default_lang`. `default_lang` MUST exist in `languages`. 302 / 400 / 409. |
| `GET /admin/dramas` | JSON list of all dramas (`created_at DESC`); fields `slug, name, default_lang, ep_count, created_at, updated_at` |
| `DELETE /admin/dramas/{slug}` | Removes drama row + translations + `OUT_DIR/{slug}/`; 409 if any episodes attached; 404 if unknown. `[gate]` can_delete |
| `POST /admin/dramas/{slug}/episodes` | multipart `video`. Auto-increment `ep_number = MAX+1`. UNIQUE-collision retry up to 3×; persistent collision → 503. 404 if drama missing. |
| `POST /admin/dramas/{slug}/episodes/{ep}` | multipart `video`. Re-encode existing episode in place. 404 if episode missing. 409 if `status=encoding`. |
| `POST /admin/dramas/{slug}/episodes/batch` | multipart `videos` (多文件). 每个文件名须以 `EP<n>` 开头（大小写不敏感）→ 集号。已存在的集走重传语义覆盖；`status=encoding` 的集跳过。返回逐文件结果 `{ok_count, error_count, results[]}`，部分失败不致命。**路由声明在 `episodes/{ep}` 之前**，否则 `batch` 字面段会被 `{ep}` 的 `^[0-9]+$` 捕获并 422。 |
| `POST /admin/dramas/{slug}/subtitles/batch` | multipart `files` (多文件). 文件名须形如 `EP<n>-<lang>-说明.vtt\|.srt`（EP 大小写不敏感）；`<lang>` 按最长匹配解析自启用语言注册表（兼容 `zh-rCN` 这类带连字符的 code）。`.srt` 自动转 WebVTT。已存在的 (集, 语言) 字幕覆盖。返回逐文件结果，部分失败不致命。 |
| `GET /admin/episodes` | JSON list of all episode rows (`created_at DESC`); each row carries `drama_name` via JOIN |
| `GET /api/languages` | SDK: array of `{code, display_label}` for every registered language; ordered by `code ASC`; empty registry → `[]` |
| `GET /api/episodes/{slug}/{ep}` | SDK endpoint; strict `EpisodeInfo` JSON; 404 unless `status=ready` |
| `GET /api/dramas` | SDK drama catalog; `DramaSummary[]` ordered by `lastUpdatedAt DESC`; empty → `[]`; only dramas with ≥1 `ready` episode; `dramaName` sourced from `dramas.name` |
| `GET /api/dramas/{slug}/episodes` | SDK per-drama episode list; full `EpisodeInfo[]` (with `drm` embedded) ordered by `ep_number ASC`; empty → `[]`; 422 on malformed slug |
| `POST /api/episodes/{slug}/{ep}/cover` | multipart `cover`; overwrites `cover.jpg`；同时翻该集 `sync_status='dirty'` |
| `POST /admin/dramas/{slug}/sync` | 业务同步：剧入队（含其下全部 dirty / pending_delete 集）。503 当 `BUSINESS_SYNC_BASE_URL` 未设；404 / 200 (no-op) / 202。`[gate]` can_sync |
| `POST /admin/episodes/{slug}/{ep}/sync` | 业务同步：单集入队。503 / 404 / 200 (no-op) / 409 (剧从未同步) / 202。`[gate]` can_sync |
| `GET /admin/sync` | HTML 总览页：列出全部非 clean 的剧 + 集 |
| `GET /admin/sync/summary` | JSON `{enabled, non_clean_count}`：导航栏 5s 轮询用 |
| `GET /drm/{slug}/{episode_id}/key` | 16 raw bytes, `application/octet-stream` (same URL embedded in the m3u8) |
| `GET /videos/{slug}/{episode_id}/**` | static mount over `OUT_DIR`; `/videos/{slug}/keys/**` is explicitly denied by middleware |

Upload → pipeline lifecycle (all synchronous work happens in the request handler so the admin list has data to render immediately):

1. Validate path (`drama_slug ~ ^[a-z0-9][a-z0-9-]*$`; for re-upload, `ep` numeric). Drama must already exist (404 otherwise). For auto-increment (`POST /admin/dramas/{slug}/episodes`), the server computes `ep_number = MAX(ep_number)+1`. For re-upload (`POST /admin/dramas/{slug}/episodes/{ep}`), the episode must exist and not be in `status=encoding` (409 otherwise).
2. Stream upload to `UPLOAD_TMP_DIR/upload-<uuid>.mp4`.
3. `ffprobe` → `duration_ms` + `width` + `height`（源视频 codec dimension；用于推导 `EpisodeInfo.videoTracks` 每档的 width / height）。
4. `ffmpeg -ss 0 -vframes 1 -vf scale=-2:720` → `OUT_DIR/{slug}/ep-{n}/cover.jpg`（cover 不随版本变，始终写 v1 目录）。
5. Upsert `episodes` row: `status=pending`, `episode_id="{slug}-ep-{n}"`, cover URL set. **reupload-versioning**: 新集 `upload_version=1`；重传时该列自增（v2 / v3 …），upsert 返回 `(old_source, new_version)`，路由把版本号塞进 `Job.upload_version` 并写一条 `episode_uploads` 记录（含上传者、源文件名、时间戳）。
6. Enqueue job on the global `asyncio.Queue` and 302 back to `/admin`.
7. A pool of `PIPELINE_CONCURRENCY` worker coroutines (default 2) pulls from the queue; jobs for different episodes run in parallel, jobs for the same `episode_id` are serialized by a per-episode `asyncio.Lock` in `queue.py`. Each worker computes `ep_dir = db.episode_ep_dir(ep_number, job.upload_version)`（v1 → `ep-{n}`，v2+ → `ep-{n}-v{V}`），flips `status=encoding`，runs `pipeline.sh <tmp> {OUT_DIR}/{slug} {ep_dir} /drm/{slug}/{ep_dir}/key`（第 4 个参数是写进 `#EXT-X-KEY:URI` 的相对路径，verbatim），reads `{OUT_DIR}/{slug}/keys/{ep_dir}.key.b64` + `.iv`, sets `status=ready` 同时把 `play_url` / `key_uri` 写成本次版本的路径。On non-zero exit: `status=failed` with the last 4 KiB of stderr in `error_message` (artifacts under `OUT_DIR` are NOT auto-cleaned — kept for post-mortem). Temp upload file is always removed.
8. On process restart any row left in `status=encoding` is flipped to `failed` with `error_message="orphaned by restart"`.

**reupload-versioning（重传路径版本化）**：客户端 AES-128-CBC 解密用错 key 不会冒一个干净的「key mismatch」错——会拿新 key 解旧 segments，静默乱码。所以每次重传必须落到一组新路径上，让客户端缓存按 URL 自然 miss、回源拿新版本，老缓存仍能拿到老路径上还在的老切片+老 key 把这集播完。落地方式：
- `episodes.upload_version` 自增；`episode_uploads(episode_id, version, source_filename, uploaded_by, uploaded_at)` 表记录每次重传，episode 删除时 CASCADE 清掉。
- 路径派生用 `db.episode_ep_dir(ep_number, version)` 唯一来源：v1 = `ep-{n}`（不打扰存量数据）；v2+ = `ep-{n}-v{V}`。本地目录、桶 staging/prod 前缀、`#EXT-X-KEY:URI`、`videoTracks[].url`、`drm.keyUri` 全部带这个后缀；DRM router 的段 pattern (`^[a-z0-9][a-z0-9-]*$`) 已能匹配两种形态，无需改动。
- Cover.jpg + subtitles 不版本化，固定在 v1 目录下（不加密，无 key-mismatch 风险）。
- 旧版本**不会自动清理**：留在本地 + staging/prod 桶里，等过渡期老客户端用完。删除整集 (`DELETE /admin/episodes/...`) 会一次性扫掉所有版本的本地目录 / 密钥 / staging 前缀；prod 由 sync 触发 `unpublish_episode_from_prod` 按版本列表挨个清。后续如需 GC 仅留最近 N 版,需另写脚本（目前没有）。
- 同一集的并发重传(队列里堆了 v2 + v3 两个 Job 时)由 `_episode_locks` 串行化；Job 自己捕获了版本号,不会被后入队的 upsert 反向改写。

Schema changes are destructive across the `drama-as-entity` deploy: delete `hls.db` before redeploy. There is no production data to preserve. See `openspec/specs/drama-entity/` (after archival) or `openspec/changes/drama-as-entity/` (in flight) for the spec.

`pipeline.sh` and the three stage scripts are invoked unchanged — the service only orchestrates them.

## Direct pipeline commands (fallback / debugging)

```bash
./pipeline.sh <source.mp4> <output_dir> <episode_id> <key_uri>
# stages individually:
./generate-drm-key.sh <episode_id> <output_dir>/keys
./encode-clear.sh     <source> <rung_dir> <ladder> <height> <fps> <bitrate_kbps>
./encrypt-segments.sh <rung_dir> <ladder> <episode_id> <key_dir> <key_uri>
```

Sanity-decrypt a segment (same hint `pipeline.sh` prints at the end):

```bash
openssl enc -d -aes-128-cbc \
  -K "$(xxd -p -c 32 out/<slug>/keys/ep-1.key)" \
  -iv "$(cat out/<slug>/keys/ep-1.iv)" \
  -in out/<slug>/ep-1/720p/seg-720p-0.m4s | ffprobe -i -
```

System prerequisites: `ffmpeg`, `ffprobe`, `openssl`, `xxd`, `awk`, `python3` (3.10+). No automated test suite — verify by uploading through `/admin` and playing the resulting media playlist in a hls.js page, VLC, or the Android SDK.

**Deployment posture**: the `/admin` console requires a login session (`admin-accounts-auth`) — operators sign in, and per-account `can_delete` / `can_sync` flags gate the destructive / production-affecting routes. The SDK-facing surface — `/api/*`, `/drm/*` (incl. the raw DRM key), `/videos/*` static files — has **no auth** and must still stay behind VPN / internal network. The login layer adds accountability and per-person permissions; it is not a substitute for the network boundary.

**账号引导与锁死自救**：fresh deploy 第一次启动，`users` 表为空 → `init_db()` 用 `ADMIN_INITIAL_PASSWORD` 建 `admin` 账号（`must_change_pw=1`，首次登录强制改密）。两个必填 env：`SESSION_SECRET_KEY`（永远必填，签名会话 cookie）、`ADMIN_INITIAL_PASSWORD`（仅首启需要）。若把自己锁在外面（忘了密码 / 丢了 `SESSION_SECRET_KEY`）：服务器在内网，有 shell 的操作员可以 (a) 用 `passlib` 重算一个 `bcrypt` hash 直接 `UPDATE users SET password_hash=... WHERE username='admin'`，或 (b) `DELETE FROM users`（仅 users 表）后带 `ADMIN_INITIAL_PASSWORD` 重启，重新触发引导。`users` / `audit_log` 两张表是 `CREATE TABLE IF NOT EXISTS` 增量加表，不影响既有剧数据，无需删 `hls.db`。

## Pipeline architecture

Three stages, chained by `pipeline.sh`, one ladder rung at a time. Keep them separate — do not fold them back into a single `ffmpeg` invocation.

- **Stage 0 — `generate-drm-key.sh`**: per-episode DRM material. Writes three sibling files with a fixed naming contract used by every downstream consumer:
  - `<ep>.key` — raw 16 bytes, server-held only.
  - `<ep>.iv` — 32 hex chars, written verbatim into `#EXT-X-KEY:IV`.
  - `<ep>.key.b64` — base64 of the key, shipped as `EpisodeInfo.drm.keyBase64`.
- **Stage 1 — `encode-clear.sh`**: FFmpeg encodes to **clear** CMAF (`init-<ladder>.mp4` + `seg-<ladder>-*.m4s` + `media-<ladder>.m3u8`). Fixed 2s GOP / 2s segments / constant FPS across rungs, `-hls_flags independent_segments+program_date_time`, `-hls_playlist_type vod`. **FFmpeg's `-hls_key_info_file` path is intentionally disabled here** because of fMP4+AES bugs — do not re-enable it; do encryption in stage 2 instead. The script resolves `SOURCE` to an absolute path before `cd`-ing into the output dir; preserve that when editing.
- **Stage 2 — `encrypt-segments.sh`**: AES-128-CBC + PKCS7 (`openssl enc` default — do **not** pass `-nopad`) in place over every `seg-<ladder>-*.m4s`, then `awk`-injects a single `#EXT-X-KEY:METHOD=AES-128,URI="…",IV=0x…` line **after** `#EXT-X-MAP:` and **before** the first `#EXTINF:`. Placement matters: `init.mp4` must stay outside the key's scope, which is what ExoPlayer's `Aes128DataSource` expects. `init.mp4` is never encrypted.

Ladder rung metadata (`NAME HEIGHT FPS BV_kbps`) lives in the `LADDERS` array in `pipeline.sh` — edit it there, not inside the stage scripts.

## 桶存储双 host 拓扑（含 staging / prod 双前缀）

设 `STORAGE_PROVIDER=oss` 或 `tos` 后，资源被分到两个 host；所选桶内部进一步分成 **staging** 和 **prod** 两个并列前缀（同一个 bucket，凭证共享）：

| 资源 | 哪台 host | m3u8 / API 里写什么 |
|---|---|---|
| `media-{rung}.m3u8` | 业务 host（这台 HLS 服务器） | API `videoTracks[].url` 写相对路径 |
| `#EXT-X-KEY:URI` | 业务 host（同 m3u8） | 相对路径 `/drm/...`，与 `EpisodeInfo.drm.keyUri` verbatim 一致 |
| `init-{rung}.mp4` | **桶 staging 前缀**（这台服务器写）／ **桶 prod 前缀**（业务服务器读） | 本地 m3u8 写**相对文件名** `init-720p.mp4`（HLS 预览页通过 `/videos/` 读本地切片，绕桶）；sync 给业务服务器的 m3u8 里 `#EXT-X-MAP:URI` + segment 行是 **prod 对象 key**（`Drama/prod/...`，无 host），业务端按自己 `MEDIA_BASE_URL` 拼前缀 |
| `seg-{rung}-N.m4s` | **桶 staging 前缀** ／ **桶 prod 前缀** | 同上 |
| `poster/{lang}.{ext}` | **桶 staging 前缀** ／ **桶 prod 前缀** | sync 时 `publish_poster_to_prod` server-side copy；payload `translations[lang].poster_url` 是 prod 绝对 URL |
| `cover.jpg` | **桶 staging 前缀** ／ **桶 prod 前缀** | sync 时 `publish_cover_to_prod` server-side copy；payload `cover_url` 是 prod 绝对 URL |
| `subtitles/{lang}.vtt` | **桶 staging 前缀** ／ **桶 prod 前缀** | sync 时 `publish_subtitle_to_prod` server-side copy；payload `subtitles[].url` 是 prod 绝对 URL |
| `*.key` / `*.iv` / `*.key.b64` | 业务 host（密钥不上桶） | 三件套不暴露 URL；`drm.keyBase64` 走 sync payload 内联 |

桶布局（不分厂商，layout 一致）：

```
<bucket>/
  Drama/staging/{slug}/poster/{lang}.{ext}              ← 海报
  Drama/staging/{slug}/{ep_dir}/cover.jpg               ← 集封面
  Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt    ← 字幕
  Drama/staging/{slug}/{ep_dir}/{ladder}/init-{ladder}.mp4 + seg-*.m4s   ← 切片
  Drama/prod/...                                        ← 镜像同结构
```

OSS 时 `<bucket>` = `photobundle`（`oss-ap-southeast-1.aliyuncs.com`）；TOS 时 `<bucket>` = `coocent-drama`（`tos-ap-southeast-1.volces.com`）。两个厂商在 `app/publish.py` 看来等价，靠 `app/storage/` 抽象切换。

`publish_ladder` / `upload_poster_to_staging` / `upload_cover_to_staging` / `upload_subtitle_to_staging`（admin 路由 + worker 在编码 / 上传成功后调用）只写 staging。**`publish_ladder` 不改写本地 m3u8** —— 本地 m3u8 保持 encode-clear.sh 产出的相对文件名形态，HLS 预览页直接走 `/videos/` 静态挂载读本地切片。`publish_*_to_prod` 系列（sync worker 调用）通过桶的 server-side copy 把对应资产从 staging 拷到 prod；**返回的是 prod 对象 key**（例如 `Drama/prod/{slug}/poster/{lang}.{ext}`），不是绝对 URL —— 业务端持有自己的 `MEDIA_BASE_URL`（TOS 直链或 CDN 域名）拼出最终 URL，CDN 友好。`publish_ladder_to_prod` 额外返回 prod-flavored m3u8 文本：用 `rewrite_playlist` 给本地相对 m3u8 的每个 init / segment 行前缀拼 `{prod_prefix}/{slug}/{ep_dir}/{ladder}`，输出 path-only 形态。`#EXT-X-KEY:URI="/drm/..."` 是相对路径不被命中、原样保留。删除时对称：staging 由 `DELETE /admin/...` 同步清；prod 由 sync worker 在删除同步成功后通过前缀级 `unpublish_episode_from_prod` / `unpublish_drama_from_prod` 单次扫除。

**本地 m3u8 形态**（HLS 预览播放器消费的版本，相对文件名，走 `/videos/` 读 OUT_DIR）：

```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="init-720p.mp4"
#EXT-X-KEY:METHOD=AES-128,URI="/drm/zhetian/ep-1/key",IV=0x...
#EXTINF:2.000000,
seg-720p-0.m4s
#EXTINF:2.000000,
seg-720p-1.m4s
#EXT-X-ENDLIST
```

**sync 推给业务服务器的同一档 m3u8**（path-only，业务端拼 `MEDIA_BASE_URL` 后才返客户端）：

```m3u8
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="Drama/prod/zhetian/ep-1/720p/init-720p.mp4"
#EXT-X-KEY:METHOD=AES-128,URI="/drm/zhetian/ep-1/key",IV=0x...
#EXTINF:2.000000,
Drama/prod/zhetian/ep-1/720p/seg-720p-0.m4s
#EXTINF:2.000000,
Drama/prod/zhetian/ep-1/720p/seg-720p-1.m4s
#EXT-X-ENDLIST
```

注意事项：

- **桶 CORS** 必须允许 GET 来自业务 host 的 Origin，否则 hls.js / 浏览器播放会被 CORS 拒（ExoPlayer / iOS 原生 HLS 不走 CORS，不受影响）。两个前缀共用一套 CORS；切到 TOS 时记得在 TOS 控制台也配同样规则。
- **AK/SK 走 gitignore 的 `app/storage/credentials.py`**（4 个常量 `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`），该文件不进 git——fresh clone 后 `cp app/storage/credentials.example.py app/storage/credentials.py` 再填自己的密钥（按人分配）。文件缺失时 provider import 抛带提示的 `RuntimeError`。endpoint / bucket（OSS `photobundle` / TOS `coocent-drama`）不是密钥，仍硬编码在各自 provider。前缀常量 (`Drama/staging` / `Drama/prod`) 在两个 provider 里独立写，两边保持一致。
- **本地切片不会被自动删**：上传到桶成功后 `OUT_DIR/{slug}/ep-{n}/`（及 `ep-{n}-v{V}/`）仍保留。`DELETE /admin/episodes/{slug}/{ep}` 会按 `episode_uploads` 列出的所有版本一并清掉本地目录 + staging 桶对象（warnings 收集失败项）；prod 桶对象由后续 sync 触发的 `unpublish_episode_from_prod` 按版本列表挨个清。重传产生的旧版本本身不会自动 GC,要靠 episode 删除统一回收。
- **切换 provider 时桶内已有的对象不会自动迁移**。从 OSS 切到 TOS（或反向）要先手工把旧桶里 `Drama/staging` + `Drama/prod` 全量复制到新桶，或者跑 `scripts/migrate_to_oss.py` 重新发布（脚本现在跟随 `STORAGE_PROVIDER` 走，会向当前选中的桶写）。

### Manual sync — staging→prod 拷贝原语（`app/publish.py`）

供 sync worker 消费（不要在 router 直接调）：

**Prod publish（staging→prod server-side copy）**：
- `publish_ladder_to_prod(slug, ep_dir, ladder) -> str`：返回 prod-flavored m3u8 文本。
- `publish_poster_to_prod(slug, lang, ext) -> str`：返回 prod URL。
- `publish_cover_to_prod(slug, ep_dir) -> str`：返回 prod URL。
- `publish_subtitle_to_prod(slug, ep_dir, lang) -> str`：返回 prod URL。

**Prod unpublish（前缀清理）**：
- `unpublish_episode_from_prod(slug, ep_dir)`：单次扫除该集 prod 端**全部对象**（cover + 字幕 + 三档 ladder）。episode delete-sync 用。
- `unpublish_drama_from_prod(slug)`：扫除整部剧 prod 端全部对象。drama delete-sync 用。
- `unpublish_ladder_from_prod` / `unpublish_poster_from_prod` / `unpublish_subtitle_from_prod`：单档 / 单海报 / 单字幕级别 prod 清理（备用，sync worker 当前不直接用，但 OpenSpec 接口里保留）。

**Staging upload（admin / api routers 在写盘后调用）**：
- `upload_poster_to_staging(slug, lang, local_path) -> str`：返回 staging URL。
- `upload_cover_to_staging(slug, ep_dir, local_path) -> str`：返回 staging URL。
- `upload_subtitle_to_staging(slug, ep_dir, lang, local_path) -> str`：返回 staging URL。
- `publish_ladder` 是切片+m3u8 改写的复合操作（worker 调用，OpenSpec 里仍称 publish 而非 upload）。

**Staging unpublish**：
- `unpublish_episode_from_staging` / `unpublish_drama_from_staging`：DELETE 路由直接调（前缀级，覆盖所有资产类型）。
- `unpublish_poster_from_staging` / `unpublish_subtitle_from_staging`：单海报 / 单字幕级别清理（替换前 / 删除时用）。

### Migration（从单 host / 旧扁平前缀升级）

如果在启用 staging/prod 拆分之前已经在桶上有 `Drama/{slug}/...` 旧前缀对象：

```bash
STORAGE_PROVIDER=oss ./venv/bin/python scripts/migrate_to_oss.py   # 或 STORAGE_PROVIDER=tos
```

脚本扫所有 `status=ready` 行 → 重传切片到当前选中桶的 staging 前缀 → 改写本地 m3u8。**旧前缀对象不删**，仅打日志列举，操作员在控制台手动清理。幂等，可重复跑。文件名带 `_to_oss` 是历史遗留，OSS / TOS 都能用。

## 业务服务器同步

这台 HLS 服务器是 **staging editor**：所有上传 / 翻译 / 标签编辑都先落在这里，操作员预览满意后**手动点击同步**才会推到业务服务器（prod）。同步过程是单 worker FIFO 队列 (`app/sync.py`)。

### 状态机

每个 drama 行和每个 episode 行各自独立带 `sync_status` 列：

```
dirty   ─→ syncing ─→ clean
              │
              └─→ sync_failed
pending_delete ─→ syncing ─→ (row 物理删除 + prod OSS 清理)
                      │
                      └─→ sync_failed (intent 仍是 pending_delete)
```

- 新建 drama / 上传集 → `dirty`。
- 编辑 drama 翻译 / 海报 / 标签 / 演员 → 该 drama 行 `dirty`（不影响其下集）。
- 上传字幕 / 替换封面 / 重传视频 → 该集行 `dirty`（不影响 drama 行）。
- Library 级联：tag PATCH / translation upsert/delete → 引用该 tag 的全部 drama 翻 dirty；actor 同；语言 `display_label` 改 → 该语言下有字幕的全部 drama 翻 dirty。
- 进程崩溃后启动期 reap：`sync_status='syncing'` 的行被翻成 `sync_failed` + 错误 `"orphaned by restart"`。

### 双阶段删除

`DELETE /admin/episodes/{slug}/{ep}` 和 `DELETE /admin/dramas/{slug}` 现在按 `last_synced_at` 分支：

- **从未同步过**（`last_synced_at IS NULL`）→ 物理删行 + 本地清 + staging OSS 清。
- **同步过**（`last_synced_at` 非空）→ 行**保留**，`sync_status='pending_delete'`；本地清 + staging OSS 清照旧。下一次 sync 触发对业务服务器的 `DELETE /sync/episodes/{slug}/{ep}` (或 drama 变体)，业务服务器返回 2xx 后才物理删行 + 清 prod OSS。

`pending_delete` 行在剧详情页隐藏，但出现在 `/admin/sync` 总览页，让操作员知道还有挂起的同步。

### 操作员 UX

| 入口 | 行为 |
|---|---|
| 导航栏 `<div id="sync-zone">` | 5s 轮询 `GET /admin/sync/summary`，显示"需同步: N"链接到 `/admin/sync`。同步禁用时不显示。 |
| 剧详情页"[同步整部剧]" | 入队 drama 同步 + 该剧下全部 dirty/pending_delete 集；剧 `last_synced_at` 更新后，子集逐个跑。 |
| 集详情页"[同步本集]" | 入队该集同步。前置：剧必须至少同步过一次（否则 409）。 |
| 首页卡片角标 | "非 clean: N" 角标（drama dirty + 子集 non-clean 计数）。 |
| `/admin/sync` 总览 | 列出全部非 clean 的剧 + 集；"[同步全部]" 按钮按行入队全部同步。 |

失败行显示红色 `sync_failed` 徽章（鼠标 hover 看错误详情）；点"同步本集"重试。

### 业务服务器线协议（`/sync/*`）

所有请求带 `X-API-Key: <BUSINESS_SYNC_API_KEY>`。共 4 个端点（业务服务器侧实现）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/sync/dramas` | upsert drama + translations + tags + actors + languages 数组（业务服务器再异步从 staging 拉海报字节）。 |
| DELETE | `/sync/dramas/{slug}` | 删一部剧的全部数据。 |
| POST | `/sync/episodes` | upsert 一集；body 含三档 ladder 的 prod-flavored m3u8 文本、DRM key/iv、cover URL、subtitle URL 列表。 |
| DELETE | `/sync/episodes/{slug}/{ep}` | 删一集。 |

详细 payload schema 见 `openspec/changes/business-server-sync/design.md` 的"Decision: POST /sync/dramas payload shape"小节。

### 部署须知

- 这台 HLS 服务器需要能 HTTP 访问业务服务器（同 VPN / 同内网）。
- 业务服务器 + HLS 服务器**共用同一个 OSS bucket**：staging 前缀只这台写、业务服务器只读 prod 前缀。同步即"server-side copy + 通知"。
- 整个 sync 链路**没有自动重试**：失败 → 红色徽章 + 错误文本，操作员点"重试"再触发一次。

## Cross-system contracts to preserve

- **`#EXT-X-KEY:URI` ↔ `EpisodeInfo.drm.keyUri` must match verbatim** (query string, fragment, everything). The SDK uses it as the `DrmKeyStore` lookup key. If one changes, the other must change in lockstep. （OSS 启用时这条契约不变 —— 两边仍是业务 host 的相对路径 `/drm/{slug}/ep-{n}/key`。）
- **`#EXT-X-MAP:URI` 与 segment 行**与任何 API 字段都无 verbatim 比对关系：它们仅供播放器消费。API 侧每档只暴露 `videoTracks[].url`（即 `media-{rung}.m3u8` 本身），不暴露 init / segment 级 URL。
- **Playlist ordering**: `#EXT-X-MAP` (init) → `#EXT-X-KEY` → first `#EXTINF`. Reordering breaks ExoPlayer decryption. `encrypt-segments.sh`'s awk program enforces this; keep it.
- **`episode-info-schema.json`** is the source of truth for the SDK contract. Required fields: `episodeId`, `durationMs`, `videoTracks`. `videoTracks` 始终带全三档 rung（`id` = `high` / `mid` / `low` → 1080p / 720p / 540p），每档含 `url` + `width` / `height`。`drm.keyBase64` decoded length must be exactly 16 bytes; `drm.ivHex` is optional and falls back to `#EXT-X-KEY:IV`. `videoTracks[].width` / `height` 是该档 encoded codec dimension，由源视频尺寸按 `scale=-2:HEIGHT` 推导；老 ready 行（升级前源尺寸缺失）该两字段为 `null`，SDK 拿到 null 时退化到首帧解码后再读宽高。
- **No master playlist / no in-player ABR.** Each `videoTracks[].url` is a standalone single-rung media playlist; the SDK picks a rung client-side from the `videoTracks` array. Do not introduce `#EXT-X-STREAM-INF` logic or a master manifest.

## Spec-driven workflow

`openspec/` uses the OpenSpec workflow (config at `openspec/config.yaml`, plus the `openspec-*` / `opsx:*` skills). `encode-clear.sh` references `openspec/changes/short-drama-fast-startup/specs/shortdrama-server/spec.md` as the authoritative rationale for disabling FFmpeg's native HLS-AES path; that spec file isn't currently in the tree (only `openspec/changes/archive/` exists). When making behavior-level changes to the pipeline, prefer routing through the OpenSpec propose/apply flow rather than editing scripts directly.
