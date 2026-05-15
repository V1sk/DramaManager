## Why

视频切片走 OSS staging/prod 双前缀（业务服务器从 OSS 直读、客户端走 OSS / CDN），但**海报 / 封面 / 字幕**目前还停留在 HLS 服务器本地磁盘 + `/videos/...` 静态挂载，业务服务器在收到 `/sync/*` 时需要 HTTP 主动拉取字节再落到自己磁盘。

这个不对称是 step 6（`business-server-sync`）的最小变更副产物，不是合理设计：

- **网络耦合**：业务服务器必须能直连 HLS 服务器，HLS 服务器不能纯内网部署。
- **协议复杂**：`POST /sync/*` 多出"业务服务器拉字节失败 → 502"这条路径，errno / 重试语义膨胀。
- **CDN 不友好**：海报 / 封面是客户端高频展示的小图，应该走 OSS 加速；现在每次都从业务服务器本地磁盘 serve。
- **职责漂移**：业务服务器既要存元数据又要存二进制资产，职责模糊。

把这三类资源对齐到视频切片的处理方式：HLS 端在写盘时**同时上传 OSS staging**，sync 时**OSS 服务端拷贝到 prod**，payload 直接给 prod 端绝对 URL，业务服务器只记 URL 不取字节。

## What Changes

### OSS 布局扩展

```
photobundle/
  Drama/staging/{slug}/poster/{lang}.{ext}             ← NEW（drama 海报）
  Drama/staging/{slug}/{ep_dir}/cover.jpg              ← NEW（集封面）
  Drama/staging/{slug}/{ep_dir}/subtitles/{lang}.vtt   ← NEW（字幕）
  Drama/staging/{slug}/{ep_dir}/{ladder}/init-*.mp4    ← 已有（视频 init）
  Drama/staging/{slug}/{ep_dir}/{ladder}/seg-*.m4s     ← 已有（视频 segment）
  Drama/prod/...                                        ← 镜像同结构
```

### 上传 / 删除路径

- 操作员上传海报 (`POST /admin/dramas/{slug}/poster?lang=...`) → 本地写盘 + 立即上传 staging OSS。
- 集封面：pipeline 中由 ffmpeg 提取 → 本地写盘 + 立即上传 staging。
- 集封面替换 (`POST /api/episodes/{slug}/{ep}/cover`) → 同。
- 字幕上传 (`POST /admin/episodes/{slug}/{ep}/subtitles?lang=...`) → 本地写盘 + 立即上传 staging。
- 删除海报 / 集封面 / 字幕 → 同时删 staging OSS 对象。
- 删除 drama / 删除 episode 时的 staging OSS 清理（已有 `unpublish_episode_from_staging` / `unpublish_drama_from_staging`）天然包含这些新增对象，无需另写。

### 同步路径

- 同步 drama 时：海报 staging→prod server-side copy（每个语言一个对象）。
- 同步 episode 时：集封面 + 全部字幕 staging→prod server-side copy。
- 同步 delete 时：`unpublish_*_from_prod` 已经按前缀删，自动覆盖这些新增对象。

### Wire 协议变更（**BREAKING**）

`POST /sync/dramas` 和 `POST /sync/episodes` 的 payload 字段语义改变：

- `translations[lang].poster_url`：从相对路径 `/videos/...` 改为**绝对 prod OSS URL**。业务服务器 SHALL 不再 HTTP GET 拉取字节；记录 URL 即可，客户端直连 OSS。
- `cover_url`：同上。
- `subtitles[].url`：同上。

业务服务器侧的处理流程从"收到 sync → 拉字节 → 落盘 → upsert 行"简化为"收到 sync → upsert 行（含 URL 字段）"。502 状态码（拉取失败）从协议中删除。

`/sync/episodes` 的 `playlists.{ladder}` 字段（完整 m3u8 文本）**不变** —— 它已经是 prod URL 形态。

### `EpisodeInfo` SDK 契约

由于 SDK 客户端走业务服务器的 `/api/*`，业务服务器现在可以让 SDK 的 `coverUrl` / `posterUrl` / `subtitles[].url` 直接吐 OSS prod 绝对 URL。HLS 端的 `/api/*`（调试用）不动，仍然吐相对路径（指向 staging OSS 是 follow-up）。

### 不在范围内（Non-Goals）

- HLS 端 `/api/*` 端点的 URL 形态切换（保持当前的相对路径行为，调试用，不是产品流量）。
- DRM key 文件转 OSS（暴露 AES key URL 是 ❌；`drm.key_base64` 仍走 sync payload 内联）。
- m3u8 文件本身放 OSS（仍由业务服务器自己存自己 serve）。
- CDN 设置 / OSS lifecycle / 私有 ACL（依然公开可读，沿用现有 CORS 规则）。

## Capabilities

### New Capabilities

无。本 change 是对既有 capabilities 的扩展，没有引入新的能力维度。

### Modified Capabilities

- `oss-staging-prod-separation`：OSS 路径布局扩展（poster / cover / subtitle 加入 staging/prod 镜像结构）；`publish_*` / `unpublish_*` 系列 helpers 扩容覆盖新增前缀。
- `drama-meta-translations`：`POST /admin/dramas/{slug}/poster` 在本地写盘后 SHALL 立即上传到 OSS staging；`DELETE` 同时清理 staging OSS。响应 / 错误语义不变。
- `episode-subtitles`：`POST /admin/episodes/{slug}/{ep}/subtitles` 写盘后 SHALL 立即上传 OSS staging；`DELETE` 同时清理。响应不变。
- `hls-management-server`：cover 提取（pipeline）和 cover 替换（`POST /api/episodes/{slug}/{ep}/cover`）写盘后 SHALL 立即上传 OSS staging。
- `business-server-sync`：`POST /sync/dramas` 和 `POST /sync/episodes` payload 中的 `poster_url` / `cover_url` / `subtitles[].url` 切换为**绝对 prod OSS URL**；删除业务服务器 URL 拉取语义和对应 502 错误码。Sync worker 在调用 `/sync/episodes` 之前先把 staging→prod server-side copy 这些资产；sync delete 流程的 `unpublish_*_from_prod` 自动覆盖这些新对象（前缀级清理）。

## Impact

- **代码**：
  - `app/publish.py`：新增 `publish_poster_to_prod(slug, lang) -> str`、`publish_cover_to_prod(slug, ep_dir) -> str`、`publish_subtitle_to_prod(slug, ep_dir, lang) -> str`（返回 prod URL，工作方式同 `publish_ladder_to_prod`）；新增 `upload_poster_to_staging` / `upload_cover_to_staging` / `upload_subtitle_to_staging`（被 router 在写盘后调用）；新增 `unpublish_poster_from_*` 等针对单个对象的 helpers（删除单个海报 / 字幕 / 替换封面时用）。
  - `app/routers/admin.py`：海报 upload/delete handler、字幕 upload/delete handler 增加 OSS 调用。
  - `app/routers/api.py`：cover 替换 handler 增加 OSS 调用。
  - `app/queue.py`：worker 在 cover 提取之后增加 OSS 上传步骤。
  - `app/sync.py`：`build_drama_payload` / `build_episode_payload` 改为吐绝对 prod URL；`handle_drama_sync` / `handle_episode_sync` 在调用业务服务器之前先 server-side copy 这些资产到 prod。
- **Schema**：无变化（DB 列保持现状；对应 URL 字段在 `episodes.cover_url` / translations `field='poster'` 仍存相对路径，作为本地落盘位置；OSS URL 由 sync worker 即时拼）。
- **外部契约**：
  - 业务服务器侧 `/sync/*` 处理流程**简化** — 不再做 URL 拉取。
  - 业务服务器侧 SDK API（`/api/*`）的 `coverUrl` / `posterUrl` / `subtitles[].url` 形态变化（变成绝对 OSS URL）— 这是业务服务器的实现细节，不在本 change 直接管，但需要在文档里更新 `docs/business-server-integration.md` 提示。
  - 客户端 SDK：m3u8 已经引用 OSS 绝对 URL，加上 cover / poster / subtitle 也变成绝对 URL，整体上 SDK 的 HTTP 端点数从"业务服务器 + OSS"变成更倾向"主体 OSS + 业务服务器只做元数据 + DRM key"，CDN 命中率提升。
- **OSS 写量**：编辑频率高的资源（封面 / 字幕）每次保存多一次 OSS PUT。同一个 slug+lang 的 PUT 是覆盖（OSS 计费没有惩罚），可接受。
- **CORS**：bucket CORS 原本就允许公开 GET，新增对象类型（图片 / vtt）也满足。
- **迁移**：升级前已有的剧集需要 backfill —— 提供 `scripts/backfill_assets_to_oss.py` 一次性把 `/videos/.../poster/`、`/videos/.../cover.jpg`、`/videos/.../subtitles/` 下所有现存文件上传到 staging。幂等。
- **文档**：更新 `docs/business-server-integration.md`（删 §5 URL 拉取 contract，第 7 节 OSS 路径表加新增项，第 12 节时序图简化业务服务器侧的拉取分支），更新 `CLAUDE.md` OSS 拓扑章节。
