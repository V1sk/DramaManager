## ADDED Requirements

### Requirement: 剧目录查询

服务 SHALL 提供 `GET /api/dramas` 端点，返回 JSON 数组 `DramaSummary[]`，其中每个元素对应 **至少有一集 `status='ready'`** 的剧。没有任何 ready 剧时服务 SHALL 返回 `200 []`。

`DramaSummary` 结构（字段名、类型、可空性是 SDK 契约）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `dramaSlug` | `string` | 剧的唯一 slug，客户端继续查单剧集数列表用 |
| `dramaName` | `string` | 展示名（中文 OK） |
| `epCount` | `integer ≥ 1` | 该剧 `status=ready` 的集数 |
| `latestEpNumber` | `integer ≥ 1` | 该剧 `status=ready` 的最大 `ep_number` |
| `posterUrl` | `string \| null` | 见下方 "封面选取规则" |
| `lastUpdatedAt` | `string` | 该剧全部 ready 行里 `updated_at` 的最大值，ISO 8601（`YYYY-MM-DDTHH:MM:SSZ`） |

**封面选取规则**：`posterUrl` 等于该剧 ready 状态集里 **`ep_number` 最小** 的那一集的 `cover_url`（通常是第 1 集；若第 1 集未 ready，则退化到 ready 集里最小编号的那一集）。若最小编号那一集的 `cover_url` 为 NULL，`posterUrl` 为 `null`。

**排序**：数组 SHALL 按 `lastUpdatedAt` 降序排列（有新集的剧排在前面）。两行 `lastUpdatedAt` 相同时 SHALL 按 `dramaSlug` 升序作为次级排序，保证稳定。

#### Scenario: 多部剧按最近更新时间降序返回
- **GIVEN** 数据库中存在三部剧：
  - `drama_slug=a`，2 集 ready（`lastUpdatedAt` 取两行的较大值 = `2026-04-20T10:00:00Z`）
  - `drama_slug=b`，3 集 ready，`lastUpdatedAt = 2026-04-24T10:00:00Z`
  - `drama_slug=c`，1 集 ready，`lastUpdatedAt = 2026-04-22T10:00:00Z`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，数组顺序是 `[b, c, a]`
- **AND** 每项都包含 `dramaSlug`、`dramaName`、`epCount`、`latestEpNumber`、`posterUrl`、`lastUpdatedAt` 六个字段

#### Scenario: 没有任何 ready 集的剧不出现在目录中
- **GIVEN** 数据库中存在一部剧 `drama_slug=newcoming`，所有行状态为 `encoding` 或 `failed`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应数组中不包含 `dramaSlug=newcoming` 的条目

#### Scenario: 全库无 ready 记录返回空数组
- **GIVEN** 数据库中没有任何状态为 `ready` 的行
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，响应体为 `[]`

#### Scenario: 封面取最小 ready ep_number 的 cover
- **GIVEN** 数据库中 `drama_slug=x` 存在：`ep_number=1` 状态 `failed`、`ep_number=2` 状态 `ready` 且 `cover_url='http://h/x/ep-2/cover.jpg'`、`ep_number=3` 状态 `ready` 且 `cover_url='http://h/x/ep-3/cover.jpg'`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 对应 `drama_slug=x` 的元素里 `posterUrl = 'http://h/x/ep-2/cover.jpg'`（ready 集里最小 ep_number 对应的那张）

### Requirement: 按剧查询已就绪集数列表

服务 SHALL 提供 `GET /api/dramas/{drama_slug}/episodes` 端点。路径参数 `drama_slug` MUST 匹配 `^[a-z0-9][a-z0-9-]*$`，不匹配时服务 SHALL 响应 HTTP 422。匹配时服务 SHALL 返回 JSON 数组，其中元素按 `ep_number` 升序排列，且仅包含数据库中 `drama_slug` 等于路径参数且 `status = 'ready'` 的行。数组元素结构 MUST 严格等同 `episode-info-schema.json` 定义的 `EpisodeInfo` —— 与 `GET /api/episodes/{drama_slug}/{ep}` 单集接口返回的单个对象字段映射、取值、序列化行为完全一致（复用同一 `row → EpisodeInfo` 构造逻辑）。

#### Scenario: 一部剧多集 ready 按集号升序返回
- **GIVEN** 数据库中 `drama_slug=langyabang` 存在 5 行：`ep_number` 分别为 1 / 2 / 3 / 4 / 5；其中 1、2、5 状态为 `ready`，3 状态为 `encoding`，4 状态为 `failed`
- **WHEN** 客户端请求 `GET /api/dramas/langyabang/episodes`
- **THEN** 响应为 200，JSON 数组长度为 3
- **AND** 数组元素按顺序对应 `ep_number=1`、`ep_number=2`、`ep_number=5`
- **AND** 每个元素包含 `episodeId`、`playUrl`、`durationMs`、`coverUrl` 以及非空的 `drm` 子对象（`keyUri`、`keyBase64`、`ivHex`）
- **AND** 整个响应可逐元素通过 `episode-info-schema.json` 校验

#### Scenario: 所有集未就绪返回空数组而非 404
- **GIVEN** 数据库中 `drama_slug=newdrama` 存在 2 行，状态分别为 `pending` 和 `encoding`
- **WHEN** 客户端请求 `GET /api/dramas/newdrama/episodes`
- **THEN** 响应为 200，响应体为 `[]`

#### Scenario: 从未见过的 drama_slug 也返回空数组
- **WHEN** 客户端请求 `GET /api/dramas/never-seen/episodes`，数据库中没有任何匹配行
- **THEN** 响应为 200，响应体为 `[]`

#### Scenario: 非法 drama_slug 被拒绝
- **WHEN** 客户端请求 `GET /api/dramas/Bad_Slug/episodes`（包含下划线、大写字母）
- **THEN** 响应为 422
- **AND** 不执行任何数据库查询

### Requirement: EpisodeInfo 响应体填充 initUrl / firstSegUrl / fallback

服务构造 `EpisodeInfo` 响应时 SHALL 填充以下字段（`episode-info-schema.json` 已定义、此前服务端省略）：

- `initUrl` = `{PUBLIC_BASE_URL}/videos/{drama_slug}/{episode_id}/720p/init-720p.mp4`
- `firstSegUrl` = `{PUBLIC_BASE_URL}/videos/{drama_slug}/{episode_id}/720p/seg-720p-0.m4s`
- `fallback.low` = `{PUBLIC_BASE_URL}/videos/{drama_slug}/{episode_id}/540p/media-540p.m3u8`
- `fallback.high` = `{PUBLIC_BASE_URL}/videos/{drama_slug}/{episode_id}/1080p/media-1080p.m3u8`

URL MUST 从 `drama_slug` + `episode_id` + `PUBLIC_BASE_URL` 直接拼出，不依赖读文件系统或 DB 额外列。单集端点、按剧集数列表端点（以及任何未来复用 `_row_to_episode_info` 的端点）MUST 在同一次响应里返回结构完全一致的这些字段。

#### Scenario: 单集响应包含四个推导 URL
- **GIVEN** `PUBLIC_BASE_URL=http://hls.internal:8000`，数据库中存在 `drama_slug=ly, ep_number=3, status=ready` 的行（`episode_id=ly-ep-3`）
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** 响应体中：
  - `initUrl = "http://hls.internal:8000/videos/ly/ly-ep-3/720p/init-720p.mp4"`
  - `firstSegUrl = "http://hls.internal:8000/videos/ly/ly-ep-3/720p/seg-720p-0.m4s"`
  - `fallback.low = "http://hls.internal:8000/videos/ly/ly-ep-3/540p/media-540p.m3u8"`
  - `fallback.high = "http://hls.internal:8000/videos/ly/ly-ep-3/1080p/media-1080p.m3u8"`
- **AND** 该响应体可通过 `episode-info-schema.json` 校验

#### Scenario: 按剧集数列表中每集都包含这四个字段
- **GIVEN** 同一个 ready 行
- **WHEN** 客户端请求 `GET /api/dramas/ly/episodes`
- **THEN** 数组中 `ep_number=3` 对应的元素四个字段与单集接口的响应逐字节一致

### Requirement: API 响应与 m3u8 使用 host-relative URL

服务在所有 API 响应（`EpisodeInfo` 所有 URL 字段：`playUrl` / `coverUrl` / `initUrl` / `firstSegUrl` / `fallback.low` / `fallback.high` / `drm.keyUri`；`DramaSummary.posterUrl`）和 m3u8 里的 `#EXT-X-KEY:URI` 中 MUST 使用 host-relative 相对路径（形如 `/videos/{slug}/ep-{n}/...` 或 `/drm/{slug}/ep-{n}/key`），不允许包含 scheme / host。`drm.keyUri` 与 m3u8 里的 `#EXT-X-KEY:URI` MUST 字节级一致（verbatim 契约不变）。服务 SHALL NOT 依赖 `PUBLIC_BASE_URL` 环境变量；该变量已废弃。

#### Scenario: 所有 URL 以 `/` 开头且不含 scheme
- **GIVEN** 一条 `status=ready` 的剧集记录
- **WHEN** 客户端请求 `GET /api/episodes/{slug}/{ep}`
- **THEN** 响应里 `playUrl`、`coverUrl`、`initUrl`、`firstSegUrl`、`fallback.low`、`fallback.high`、`drm.keyUri` 每个字段 MUST 以 `/` 开头、MUST NOT 以 `//` 开头、MUST NOT 包含 `://`

#### Scenario: drm.keyUri 与 m3u8 里的 #EXT-X-KEY:URI verbatim 一致
- **GIVEN** 一条 `status=ready` 的剧集记录
- **WHEN** 客户端分别获取 `drm.keyUri` 和 m3u8 里 `#EXT-X-KEY:URI=...` 的字符串
- **THEN** 两者字节级相等

### Requirement: URL 段 / 磁盘目录 / key 文件名使用短形式 `ep-{n}`

服务构造 URL 段、磁盘目录、pipeline 第 3 参数、key 文件名前缀时 MUST 使用短形式 `ep-{ep_number}`。DB 的 `episode_id` 列（对外暴露为 `EpisodeInfo.episodeId`，用作 SDK 全局唯一 id）MUST 保留完整形式 `{drama_slug}-ep-{ep_number}`。两种形式各司其职：`episodeId` 是 SDK 契约，`ep-{n}` 是文件系统 / URL 段约定。

#### Scenario: /drm 端点可拿到 16 字节 key
- **GIVEN** 磁盘上存在 `{OUT_DIR}/{slug}/keys/ep-{n}.key` 文件
- **WHEN** 客户端按 API 响应中的 `drm.keyUri = "/drm/{slug}/ep-{n}/key"` 发起 GET
- **THEN** 响应为 200，`Content-Type: application/octet-stream`，响应体长度为 16 字节，内容与 `ep-{n}.key` 文件字节一致

#### Scenario: /videos 下的集段路径是 ep-{n}，EpisodeInfo.episodeId 是 {slug}-ep-{n}
- **GIVEN** 一条 `drama_slug=ly, ep_number=3, status=ready` 的剧集记录
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** `episodeId = "ly-ep-3"`
- **AND** `playUrl = "/videos/ly/ep-3/720p/media-720p.m3u8"`
- **AND** `drm.keyUri = "/drm/ly/ep-3/key"`

#### Scenario: /drm 端点同时接受旧格式完整 episode_id 段（向后兼容）
`/drm/{drama_slug}/{episode_id}/key` 的 `episode_id` pattern MUST 与 `drama_slug` pattern 一致（`^[a-z0-9][a-z0-9-]*$`）；handler 直接使用该段作为 key 文件名前缀，不做归一化。这样 bug 修复前产生的磁盘布局（`out/{slug}/keys/{slug}-ep-{n}.key` + m3u8 里写 `/drm/{slug}/{slug}-ep-{n}/key`）继续可用，避免强制重传现存数据。

- **GIVEN** 磁盘上存在 `out/oldslug/keys/oldslug-ep-7.key` 文件（bug 修复前产出）
- **WHEN** 客户端请求 `GET /drm/oldslug/oldslug-ep-7/key`
- **THEN** 响应 200，返回该文件的 16 字节内容
- **AND** 同时 `GET /drm/newslug/ep-3/key` 对 `out/newslug/keys/ep-3.key` 也 200（新产物仍正常工作）

### Requirement: 列表端点与单集端点共享 EpisodeInfo 构造逻辑

服务 SHALL 把 DB row 映射为 `EpisodeInfo` 对象的逻辑实现为单一的模块级 helper（例如 `_row_to_episode_info`）；`GET /api/episodes/{slug}/{ep}` 单集端点和 `GET /api/dramas/{slug}/episodes` 列表端点 MUST 同时调用该 helper 产出响应元素。未来修改 `EpisodeInfo` 的字段映射时，两个端点 MUST 行为同步变化，不允许出现漂移。

#### Scenario: 单集和列表中的同一集对象等价
- **GIVEN** 数据库中存在 `drama_slug=langyabang, ep_number=3, status=ready` 的行
- **WHEN** 客户端同时请求 `GET /api/episodes/langyabang/3` 和 `GET /api/dramas/langyabang/episodes`
- **THEN** 单集接口返回的对象与列表接口中 `ep_number=3` 对应的元素，按字段逐一比较，取值完全一致
