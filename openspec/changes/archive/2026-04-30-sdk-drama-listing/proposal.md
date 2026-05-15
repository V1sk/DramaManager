## Why

上一个 change `hls-management-server`（已归档 `2026-04-24-hls-management-server`）交付的 SDK 侧接口只能按 `(slug, ep)` 查单集。播放器端还需要两件事：

1. **拿到有哪些可看的剧**（剧目录）—— 客户端进入首页 / 浏览页时必须先知道有哪些剧、它们的名字和封面，否则拿不到 `drama_slug`，下一层的单剧集数列表查不起来。
2. **拿到某一部剧已就绪的集数列表** —— 进入剧详情页后渲染选集栏、支持连播秒开。

管理端接口 `GET /admin/episodes` 完全不能复用（返回所有状态 + 所有剧混在一起 + snake_case + 泄露 `error_message` 等内部字段，也没有剧名聚合）。

## What Changes

**新增两个面向 SDK 的端点：**

- `GET /api/dramas` — 返回所有"至少有一集 `status=ready`"的剧的摘要列表 `DramaSummary[]`，按 `lastUpdatedAt` 降序（有新集的剧排前，符合短剧平台习惯）。空目录（数据库里没有任何 ready 记录）返回 `200 []`。
- `GET /api/dramas/{drama_slug}/episodes` — 返回该剧所有 `status=ready` 的集按 `ep_number` 升序排列的 `EpisodeInfo[]`。数组元素结构严格等同 `episode-info-schema.json` 里的 `EpisodeInfo`，含 `drm` 子对象（支持连播秒开）。`drama_slug` 必须匹配 `^[a-z0-9][a-z0-9-]*$`，否则 422。空结果（剧不存在 / 所有集都非 ready）返回 `200 []`。

**所有 URL 字段返回 host-relative 相对路径**（`playUrl` / `coverUrl` / `initUrl` / `firstSegUrl` / `fallback.low` / `fallback.high` / `drm.keyUri` / `DramaSummary.posterUrl`）。客户端用自己发起请求的 host 拼回绝对 URL。废弃 `PUBLIC_BASE_URL` 环境变量 —— 不再需要，删除。m3u8 里的 `#EXT-X-KEY:URI` 同步改成相对路径（`/drm/{slug}/ep-{n}/key`），与 API 返回的 `drm.keyUri` verbatim 一致。`episode-info-schema.json` 里所有 URL 字段的 `format: uri` 改为 `format: uri-reference`（相对路径在严格 format 校验下合法）。

**修复 URL 段与磁盘目录/key 文件名不一致的 pre-existing bug**：上一个 change 里 worker 把 DB 的完整 `episode_id`（`{slug}-ep-{n}`）塞进 URL 段和 pipeline 第 3 参数；但 admin.py 的 cover URL 和 `/drm` router 的 pattern 都用短形式 `ep-{n}`，导致 `/drm/.../key` 端点永远 404/422。修复：queue.py / api.py 在构造 URL 和 pipeline 参数时统一使用 `ep-{ep_number}`；DB 里的 `episode_id` 列保留完整形式作为 SDK 契约字段。

**启用三个 Phase-2 字段**（`episode-info-schema.json` 已定义、上一 change 主动省略、现在填充）：

- `initUrl` = `{PUBLIC_BASE_URL}/videos/{slug}/{episode_id}/720p/init-720p.mp4`
- `firstSegUrl` = `{PUBLIC_BASE_URL}/videos/{slug}/{episode_id}/720p/seg-720p-0.m4s`（用于秒开 / ConnectionWarmer）
- `fallback.low` = `.../540p/media-540p.m3u8`（默认 `playUrl` 是 720p，`fallback` 是 540 和 1080 两档）
- `fallback.high` = `.../1080p/media-1080p.m3u8`

这三个字段由 `drama_slug` + `episode_id` + 固定 ladder 约定线性拼出，**不改 DB schema、不改 `pipeline.sh`、不扩 `episode-info-schema.json`**（schema 里字段本就定义好）。因为所有 SDK 端点都走同一个 `_row_to_episode_info` helper，修改 helper 后单集接口 / 按剧集数列表 / 未来任何复用 helper 的端点同时生效。

**新增数据结构 `DramaSummary`**（只在本服务的 API 响应中定义，不写进 `episode-info-schema.json`）：

```
dramaSlug       : string          // 客户端拿它继续查单剧集数列表
dramaName       : string          // UI 展示
epCount         : integer         // 已 ready 集数
latestEpNumber  : integer         // 该剧 ready 的最大 ep_number
posterUrl       : string | null   // 第 1 集的 cover；若第 1 集未 ready，则用 ready 状态里 ep_number 最小的那一集的 cover
lastUpdatedAt   : string          // 该剧全部 ready 行里最大的 updated_at（ISO 8601）
```

**实现共享**：两个端点的 `row → EpisodeInfo` 字段映射抽成模块级 helper `_row_to_episode_info`，单集接口 `GET /api/episodes/{slug}/{ep}` 同步复用，保证三处响应里同一集的字段永不漂移。

**不扩 `episode-info-schema.json`**：EpisodeInfo 结构不变。`DramaSummary` 是本服务私有的响应形态。

**不改**：DB schema、`pipeline.sh`、管理页 `/admin`、`episodes` 表结构（所需聚合数据用 SQL 从现有列推导）。

## Capabilities

### New Capabilities
- `sdk-drama-listing`: SDK 侧两类列表查询 —— 剧目录（`GET /api/dramas`，返回 `DramaSummary[]`）+ 单剧已就绪集数列表（`GET /api/dramas/{slug}/episodes`，返回 `EpisodeInfo[]`）。覆盖路径参数校验、`status=ready` 过滤、排序规则、空集合语义、`DramaSummary` 字段聚合规则（posterUrl 选取）、以及两个端点与单集端点之间的字段映射一致性。

### Modified Capabilities
<!-- 不修改上一个 change 已定义的 "SDK episode-info endpoint" requirement —— 那条仍只覆盖 GET /api/episodes/{slug}/{ep}。 -->

## Impact

- **新增代码**：
  - `app/routers/api.py`：两个新 handler（剧目录、单剧集数列表）+ 抽出的 `_row_to_episode_info` helper。
  - `app/db.py`：两个新查询函数 —— `list_ready_dramas()`（聚合出 `DramaSummary` 行）、`list_ready_by_slug(drama_slug)`（单剧已就绪集）。
  - `app/models.py`：新增 `DramaSummary` Pydantic 模型。
- **不动**：`episode-info-schema.json`、DB schema（现有 `UNIQUE(drama_slug, ep_number)` 已足以支撑查询；剧目录聚合走子查询 + `GROUP BY drama_slug`，内部系统规模无需额外索引）、管理页、pipeline。
- **文档**：`CLAUDE.md` 的 "URL map" 表新增两行。
- **对已部署实例**：纯增量。老客户端继续用单集接口；新客户端按节奏接入 `/api/dramas` + `/api/dramas/{slug}/episodes`。
