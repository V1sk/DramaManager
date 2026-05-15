## Context

`hls-management-server` change（已归档 `2026-04-24-hls-management-server`）交付了 SDK 单集接口和管理端上传/查询。本 change 补齐 SDK 侧"浏览"链路 —— 客户端在没有 `drama_slug` 的情况下先通过剧目录拿到各剧的 `dramaSlug`、`dramaName`、`posterUrl`，再进入单剧详情页用 `GET /api/dramas/{slug}/episodes` 拿到该剧全部已就绪集的 `EpisodeInfo[]`，点击某集时直接播放（DRM key 已在上一步 embed 到 `EpisodeInfo.drm` 里，秒开）。

本 change 不扩 `episode-info-schema.json`、不改 DB schema、不改 `pipeline.sh`。所有新端点都在现有 `episodes` 表上做 SQL 聚合 / 过滤得出。

## Goals / Non-Goals

**Goals:**
- 让 SDK 不再依赖外部业务后台就能自给自足地发现"哪些剧可以播"以及"每部剧有哪些集"。
- 单集接口、按剧集数列表、剧目录三处返回的同一集信息保持字段一致，不漂移。

**Non-Goals:**
- 不做分页 / 筛选 / 搜索。
- 不引入剧级封面字段（管理员上传）—— MVP 用第 1 集的 cover 代替。
- 不做跨剧扁平 `EpisodeInfo[]`（所有剧所有集混一起）—— 客户端没有可行的展示方式。
- 不改 `/admin` 管理页。

## Decisions

### D1. `DramaSummary` 单独定义，不塞进 `episode-info-schema.json`

**决策**：`DramaSummary` 是本服务私有的 API 响应形态，定义在 `app/models.py` 里作为 Pydantic 模型，不往 `episode-info-schema.json` 里加。

**理由**：`episode-info-schema.json` 是 SDK 和管理端之间关于**单集播放元数据**的契约，字段用途非常聚焦（直接喂进 `ExoPlayer.MediaItem` / `DrmKeyStore`）。剧级摘要的字段（`epCount`、`latestEpNumber`、`lastUpdatedAt`、剧名）对播放器本身没用，只对目录 UI 有用。混进去会扩大 SDK 契约面，且未来如果要改剧级聚合策略（比如加"付费剧"标记）会牵动 schema。分开更稳。

**代价**：多一个数据模型要维护。可接受。

### D2. `posterUrl` 取 "ready 集里最小 `ep_number` 的 cover"

**决策**：剧目录中每部剧的封面等于该剧 `status=ready` 行中 `ep_number` 最小那一行的 `cover_url`。若该行 `cover_url` 为 NULL，`posterUrl = null`。

**理由**：
- "从头看"是短剧场景的主导预期，第 1 集的封面最能代表这部剧。
- 第 1 集未 ready 时（例如被删、正在重传、转码失败）退化到 ready 集里最小编号的 cover 而不是 NULL，避免剧目录出现无封面条目。
- 不引入"剧级 poster 字段"是为了守住 "不改 DB schema" 的边界；真有需要（管理员想单独上传剧封面）再开新 change。

**替代方案考虑过**：
- 最新集的 cover：符合"新剧情"直觉，但短剧最新集往往是高潮截图，对新用户不友好。
- 每次取第 1 集（不管 ready 状态）：会出现 `posterUrl` 指向一张**剧集本身用户访问不到**的 cover（因为单集接口对 non-ready 返回 404），客户端点进去会发现"看得到封面、点不开播放"。避免。

### D3. 剧目录按 `lastUpdatedAt` 降序 + `dramaSlug` 升序次级排序

**决策**：主排序键 `lastUpdatedAt DESC`（新剧 / 有新集的剧排前），次级排序键 `dramaSlug ASC` 打破平手，保证排序稳定。

**理由**：短剧平台 UX 惯例。中文名字典序排序意义不大（拼音转换复杂、生僻字不稳）；`dramaSlug` 是 ASCII，字典序天然稳定，适合做次级键。

### D4. 剧目录的 SQL 用 `GROUP BY drama_slug` + 相关子查询取 poster

**决策**：

```sql
SELECT
  e.drama_slug,
  MAX(e.drama_name)    AS drama_name,      -- 剧名对齐；同 slug 应当 drama_name 一致
  COUNT(*)             AS ep_count,
  MAX(e.ep_number)     AS latest_ep_number,
  MAX(e.updated_at)    AS last_updated_at,
  (SELECT e2.cover_url FROM episodes e2
     WHERE e2.drama_slug = e.drama_slug AND e2.status = 'ready'
     ORDER BY e2.ep_number ASC
     LIMIT 1)          AS poster_url
FROM episodes e
WHERE e.status = 'ready'
GROUP BY e.drama_slug
ORDER BY last_updated_at DESC, e.drama_slug ASC;
```

**理由**：
- 子查询按 `(drama_slug, ep_number)` 走既有唯一索引，`LIMIT 1` 单行取 cover，在内部系统规模下成本可忽略。
- `MAX(drama_name)` 容忍历史行 `drama_name` 字符差异（理论上同 slug 各行应当同 drama_name，但重传覆盖时可能改中文名；取 MAX 作为确定性选择）。
- 不用 `LEFT JOIN LATERAL` / `ROW_NUMBER()` 是因为 SQLite 的跨版本兼容性 —— 子查询写法在所有我们可能遇到的 SQLite 版本上都稳。

### D5. 两个列表端点空集合均返回 `200 []`，不区分"不存在"

和上一个 change 定下的 D1 一致（参见 `2026-04-24-hls-management-server/design.md` D1）：单集端点对 `status != ready` 返回 404，但列表端点空集合返回 `[]`。两者的差异来自客户端对信号的期待：
- 单集：客户端**预期有这一集**，`404` 是"你拿错了或还没到"的明确信号。
- 列表：客户端**只是想看有什么**，空就是空，不需要 "剧不存在" vs "剧存在但没 ready 集" 的区分。

### D6. 启用 `initUrl` / `firstSegUrl` / `fallback` —— URL 由约定线性推导

**决策**：`_row_to_episode_info` helper 在构造 `EpisodeInfo` 时填充 schema 里早已定义但上一 change 主动省略的四个 URL 字段。计算规则：

```
base         = {PUBLIC_BASE_URL}/videos/{drama_slug}/{episode_id}
initUrl      = {base}/720p/init-720p.mp4
firstSegUrl  = {base}/720p/seg-720p-0.m4s
fallback.low = {base}/540p/media-540p.m3u8
fallback.high= {base}/1080p/media-1080p.m3u8
```

**理由**：
- `pipeline.sh` 里 `LADDERS` 写死了 540 / 720 / 1080 三挡，`encode-clear.sh` 固定把 init 命名为 `init-{rung}.mp4`、分片为 `seg-{rung}-%d.m4s` 从 `0` 开始。所以任何 `status=ready` 的行这五个 URL 都有效。
- 不用查文件系统、不用改 DB，helper 一次性产出。
- schema 里字段就绪、结构向后兼容（都是可空 / 可选），老客户端忽略即可。

**依赖的隐含约定（风险）**：
- 若未来 pipeline 支持可变 rung 组合（比如只做 720p、或增加 480p），这里的 `fallback` URL 会指向不存在的文件。**Mitigation**：届时需要在 `episodes` 表里记录实际产出的 rung 列表，helper 改为按该列表拼。
- `seg-720p-0.m4s` 是第一个切片的命名依赖 ffmpeg `-hls_segment_filename "seg-{rung}-%d.m4s"` 的行为。如果 `encode-clear.sh` 改了这个参数（目前看没有改的理由），`firstSegUrl` 会指错。**Mitigation**：`encode-clear.sh` 的 segment_filename 约定属于 pipeline 契约，不单独改。

**替代方案考虑过**：
- 从 `row["play_url"]` 字符串切出 `base`：能省一次拼接，但隐式依赖 `play_url` 的具体格式，偶联度更高。显式重拼更清晰。
- 把每个 URL 列到 DB 里：彻底杜绝推导风险，但代价是 DB schema 膨胀 + 每次 PUBLIC_BASE_URL 变更都要写迁移。当前规模下不划算。

### D7. 所有 URL 字段 host-relative；废弃 `PUBLIC_BASE_URL`

**决策**：API 响应（`EpisodeInfo` 全部 URL + `DramaSummary.posterUrl`）和 m3u8 里的 `#EXT-X-KEY:URI` 全部改为 host-relative 相对路径（形如 `/videos/...`、`/drm/...`）。`PUBLIC_BASE_URL` 环境变量从 `app/config.py::Settings` 彻底移除。

**理由**：
- 部署灵活：同一个服务实例可能通过多个 host 被访问（VPN 直连 IP、内网域名、反向代理），响应里硬编码单一 `PUBLIC_BASE_URL` 会让其他 host 下的客户端拉不到资源。相对路径让播放器 / 浏览器用实际请求的 host 补全，自洽。
- schema 兼容：`episode-info-schema.json` 里 URL 字段的 `format` 从 `uri` 调整为 `uri-reference`（`uri-reference = uri | relative-reference`，参见 RFC 3986），严格 format 校验下也能通过。两个 example 保持原样（例子写绝对 URL 也符合 `uri-reference`）。
- DRM fast-start 契约不变：`drm.keyUri`（相对）与 m3u8 里 `#EXT-X-KEY:URI`（同为相对，由 worker 传给 `pipeline.sh` 的第 4 参数决定）仍然 verbatim 一致；SDK DrmKeyStore 以此字符串为查找键。
- `encrypt-segments.sh` 把 worker 传入的 `key_uri` 参数原样写进 m3u8，没有任何前缀拼装，所以把相对路径传进去它就忠实写相对路径。

**代价**：
- 客户端必须保留"发起请求的 host" 作为后续 URL 前缀。对浏览器是天然行为；对 Android SDK 需要在 HTTP 客户端里显式记录。不是新负担 —— 任何 SDK 都要知道服务端 base url 才能发第一次请求。
- 失去了一个 "配置就绪检查"：之前 `PUBLIC_BASE_URL` 缺失会 fail-fast，现在启动不再强制任何 URL 相关配置。对运维其实更友好（少一个 env 要设）。

**替代方案考虑过**：
- 单集 / 列表返回绝对，m3u8 里 `#EXT-X-KEY:URI` 留相对：会破坏 DrmKeyStore 的 verbatim 匹配，弃。
- 保留 `PUBLIC_BASE_URL` 但不使用：留下死配置，容易误导运维，弃。

### D8. 统一"URL 段 / 目录名 / key 文件名前缀" 为 `ep-{n}` 短形式（修复 pre-existing bug）

**决策**：在 URL 段、磁盘目录、key 文件名里统一使用短形式 `ep-{ep_number}`；DB 里 `episode_id` 列保留完整形式 `{drama_slug}-ep-{ep_number}` 作为 SDK 契约字段（对外 `EpisodeInfo.episodeId` 值）。

**背景 / Bug**：上一个 change `hls-management-server` 实施时，`queue.py` 和 `api.py` 构造 URL 时把 `row["episode_id"]`（完整形式）塞进路径段，但：
- `app/routers/admin.py` 里 `cover_url` 和磁盘目录用的是 `f"ep-{ep_number}"`
- `app/routers/drm.py` 的路径 pattern 是 `^ep-[0-9]+$`
- `pipeline.sh` 用第 3 参数既做目录名也做 key 文件名前缀，worker 传的是完整形式导致磁盘布局是 `out/{slug}/{slug}-ep-{n}/...` 和 `out/{slug}/keys/{slug}-ep-{n}.key`
- 这和 `/drm/{slug}/{ep-N}/key` router pattern 不匹配，SDK 主动调用 `/drm/...` 永远 404/422

该 bug 在 hls-management-server 的手工 smoke 测试里未触发（操作员没 curl `/drm/.../key` 验证真实 16 bytes）。本 change 在 TestClient 里加了这一步才浮出。

**修复**：
- `queue.py::_handle_job`：新增 `ep_dir = f"ep-{job.ep_number}"`；URL 段 / pipeline 第 3 参数 / key 文件名前缀全部用 `ep_dir`；DB 查询仍用 `ep_id`（完整）。
- `api.py::_row_to_episode_info`：新增 `ep_dir = f"ep-{row['ep_number']}"`；`base` 用 `ep_dir` 拼；对外 `episodeId` 字段仍用 `row["episode_id"]`。

**边界**：现有已落盘的错误目录结构（如果以前真跑过 pipeline.sh 跑到 ready 过）不会自动迁移。为了避免强制重传已有数据，`/drm/{slug}/{episode_id}/key` 端点的 `episode_id` pattern 放宽到 `^[a-z0-9][a-z0-9-]*$`（与 `drama_slug` 一致），handler 直接用 URL 段作为 key 文件名前缀 —— 无论是新布局（文件 `ep-3.key`）还是旧布局（文件 `{slug}-ep-3.key`），m3u8 里写的 URI 都能找到对应文件。这是一个向后兼容的 hotfix，代价是多接受一种 URL 段形态；安全性由 pattern 保证（仍然不允许 `/` 或 `..`）。

API 响应（`_row_to_episode_info`）构造的 `drm.keyUri` 永远是新短形式 `/drm/{slug}/ep-{n}/key` —— 这对应新 pipeline 产物。遗留 DB 行里 `key_uri` 字段是写死的旧绝对 URL + 完整 episode_id 形式；API 把 `key_uri` 字段从 DB 原样透传到响应，所以**已有遗留记录的 drm.keyUri 仍是旧格式**——但正因为 `/drm/` router 兼容，仍然可用。想彻底迁移到新格式，重传即可（pipeline 会覆盖 DB 行和磁盘产物）。

### D9. `_row_to_episode_info` helper 物理上只有一份

把 `get_episode` 里的字段映射（`episode_id → episodeId`、`duration_ms → durationMs`、`key_uri / key_b64 / iv_hex → drm.*`、`cover_url → coverUrl`，以及 D6 新增的 `initUrl` / `firstSegUrl` / `fallback` 推导）抽进模块级 `_row_to_episode_info(row: dict) -> EpisodeInfo`。单集端点和按剧集数列表端点都调用它。放在 `app/routers/api.py` 内（不单独开文件），保持职责聚焦。

## Risks / Trade-offs

- **[Risk] DRM key 一次批量分发**：按剧集数列表返回完整 `EpisodeInfo[]` 含所有集的 `keyBase64`，一次被攻破等于整部剧的 key 都外泄 → **Mitigation**：和上一个 change 一致，依赖内网无鉴权的安全假设；未来加鉴权时这两个端点要一起覆盖。
- **[Risk] 剧名迁移 / 历史残留**：`MAX(drama_name) GROUP BY drama_slug` 在同 slug 多行 drama_name 不一致时取字典序最大的那个；边界情况，但理论上每次上传都会把 `drama_name` 写成用户填的新值（覆盖语义），实际不会出问题。
- **[Trade-off] `posterUrl` 不支持管理员单独上传**：短期用第 1 集 cover 够了；真有需求再扩。
- **[Trade-off] 剧目录没 `coverUrl`（单集级封面）、没 `durationMs`（总时长）**：目录页不需要这两个；真需要再加，不怕不兼容（追加字段向后兼容）。

## Migration Plan

纯新增端点，无迁移。已部署实例直接拉新代码重启即可。老客户端继续用单集端点；新客户端按节奏接入剧目录 → 剧集列表 → 单集的导航链。

## Open Questions

无。所有决策 proposal + spec + 本文档已覆盖。
