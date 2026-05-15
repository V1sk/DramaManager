# sdk-drama-listing

SDK 侧两类列表查询 + 全局 URL / 路径段 / 字段映射规则。归档自 `2026-04-30-sdk-drama-listing`，其中 "API 响应与 m3u8 使用 host-relative URL" 已被 `2026-04-30-oss-segment-hosting` MODIFIED 修订（双形态 initUrl / firstSegUrl）。

> Editorial note: 本 capability 内的 "EpisodeInfo 响应体填充 initUrl / firstSegUrl / fallback" 用 `{PUBLIC_BASE_URL}/...` 描述四个推导 URL 的拼接规则；该 change 的同一份 spec 后续要求以**相对路径**取代之、`PUBLIC_BASE_URL` 已废弃。两条要求并存时，**相对路径的规则胜**。读者按 "API 响应与 m3u8 使用 host-relative URL"（含 oss-segment-hosting 后续修订的双形态语义）为准。

## Requirements

### Requirement: 剧目录查询

服务 SHALL 提供 `GET /api/dramas` 端点，返回 JSON 数组 `DramaSummary[]`，其中每个元素对应 **存在于 `dramas` 表且至少有一集 `status='ready'`** 的剧。没有任何符合条件的剧时服务 SHALL 返回 `200 []`。

`DramaSummary` 结构（字段名、类型、可空性是 SDK 契约，**不变**；本要求仅改变 `dramaName` 的取值来源）：

| 字段 | 类型 | 来源 |
|---|---|---|
| `dramaSlug` | `string` | `dramas.slug` |
| `dramaName` | `string` | `translations.value` 中匹配 `(entity_type='drama', entity_id=dramas.slug, lang_code=dramas.default_lang, field='name')` 的行；通过 JOIN 取 |
| `epCount` | `integer ≥ 1` | 该剧 `status=ready` 的 `episodes` 行数 |
| `latestEpNumber` | `integer ≥ 1` | 该剧 `status=ready` 行的最大 `ep_number` |
| `posterUrl` | `string \| null` | 见下方"封面选取规则" |
| `lastUpdatedAt` | `string` | 该剧全部 ready 行里 `updated_at` 的最大值，ISO 8601 |

`dramaName` 的取值来源从 `dramas.name` 列改为 `translations` 表的默认语言行；wire-format（字段名、类型、字符串值）byte-identical（同一部剧用同一份 default_lang 名称返回相同字符串）。

注意：`drama-meta-translations` 之后 SDK 仍只看到 default-lang 名称；多语言协商（`?lang=`）由 `sdk-search-and-localization` (step 6) 引入。

**封面选取规则**（不变）：`posterUrl` 等于该剧 ready 状态集里 **`ep_number` 最小** 的那一集的 `cover_url`。注意：drama-level 多语言海报（drama-meta-translations 引入的 `OUT_DIR/{slug}/poster/{lang_code}.*`）此版本中**尚未** 用于 `DramaSummary.posterUrl`。SDK 端 drama-level 海报的暴露由 `sdk-search-and-localization` 接手。

**排序**（不变）：数组 SHALL 按 `lastUpdatedAt` 降序排列。两行 `lastUpdatedAt` 相同时 SHALL 按 `dramaSlug` 升序作为次级排序。

#### Scenario: dramaName 取自 translations 表的 default_lang 行
- **GIVEN** `dramas` 中 `slug='ly', default_lang='zh-rCN'`，`translations` 中存在 `('drama', 'ly', 'zh-rCN', 'name', '琅琊榜')`，`episodes` 中至少一集 ready
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 对应元素的 `dramaName='琅琊榜'`
- **AND** 该值通过 JOIN `translations` 取得，不依赖 `dramas` 表上任何 `name` 列

#### Scenario: 没有 default_lang name 翻译的剧仍返回（带空字符串或 fallback）
- **GIVEN** 数据库异常状态：`dramas` 中 `slug='broken', default_lang='zh-rCN'`，`translations` 中没有对应 name 行（理论上 POST `/admin/dramas` 阻止了这种状态出现，但作为防御）
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 该剧的 `dramaName` 为空字符串 `""`（既不让响应失败也不让该剧消失）

#### Scenario: 没有任何 ready 集的剧不出现在目录中
- **GIVEN** `dramas` 中存在 `slug='newcoming'`，所有相关 `episodes` 行状态为 `encoding` 或 `failed`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应数组中不包含 `dramaSlug=newcoming` 的条目

#### Scenario: 全库无 ready 记录返回空数组
- **GIVEN** `episodes` 表中没有任何状态为 `ready` 的行
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，响应体为 `[]`

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

服务构造 `EpisodeInfo` 响应时 SHALL 填充以下字段：

- `initUrl` = `{base}/720p/init-720p.mp4`
- `firstSegUrl` = `{base}/720p/seg-720p-0.m4s`
- `fallback.low` = `{base}/540p/media-540p.m3u8`
- `fallback.high` = `{base}/1080p/media-1080p.m3u8`
- `subtitles` = 该集 `subtitles` 表中所有行映射为 `[{langCode, label, url}, ...]`，按 `langCode ASC` 排序；当无任何行时为 `null`

`base` 的取值由 OSS 模式开关与字段语义共同决定（详见 "API 响应与 m3u8 使用 host-relative URL"）：相对类字段 `playUrl` / `fallback.*` 的 `base = "/videos/{drama_slug}/{ep_dir}"`；OSS 模式启用时 `initUrl` / `firstSegUrl` 的 `base` 改为 `oss_upload.oss_public_base_url + "/{drama_slug}/{ep_dir}"`。

`subtitles[*].url` 始终是相对路径 `/videos/{drama_slug}/{ep_dir}/subtitles/{lang_code}.vtt`（与 cover 同形态——OSS 模式下 step 5 才把它迁过去）。`subtitles[*].label` 取自 `languages.display_label`。

URL MUST 从 `drama_slug` + `ep_dir` + 固定 ladder 约定线性拼出，不依赖读文件系统或 DB 额外列（subtitles 字段除外，它需要 join `subtitles` 表）。单集端点、按剧集数列表端点（以及任何未来复用 `_row_to_episode_info` 的端点）MUST 在同一次响应里返回结构完全一致的这些字段，包括 `subtitles`。

#### Scenario: 单集响应包含五个推导 URL（含 subtitles）
- **GIVEN** 服务为单 host 模式（`OSS_ENABLED` 未设），数据库中存在 `drama_slug=ly, ep_number=3, status=ready` 的行，且 `subtitles` 中存在 `(ly-ep-3, en)` 和 `(ly-ep-3, zh-rCN)`
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** 响应体中：
  - `initUrl = "/videos/ly/ep-3/720p/init-720p.mp4"`
  - `firstSegUrl = "/videos/ly/ep-3/720p/seg-720p-0.m4s"`
  - `fallback.low = "/videos/ly/ep-3/540p/media-540p.m3u8"`
  - `fallback.high = "/videos/ly/ep-3/1080p/media-1080p.m3u8"`
  - `subtitles = [{"langCode":"en","label":"English","url":"/videos/ly/ep-3/subtitles/en.vtt"},{"langCode":"zh-rCN","label":"简体中文","url":"/videos/ly/ep-3/subtitles/zh-rCN.vtt"}]`
- **AND** 该响应体可通过 `episode-info-schema.json` 校验

#### Scenario: 没有任何 subtitle 时该字段为 null
- **GIVEN** 同一行 `ly-ep-3` 但 `subtitles` 表中没有对应行
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** 响应体中 `subtitles = null`

#### Scenario: 按剧集数列表中每集都包含一致的 subtitles 字段
- **GIVEN** 同一个 ready 行
- **WHEN** 客户端请求 `GET /api/dramas/ly/episodes`
- **THEN** 数组中 `ep_number=3` 对应的元素的 `subtitles` 字段与单集接口的响应字节级一致

### Requirement: API 响应与 m3u8 使用 host-relative URL

> Modified by `2026-04-30-oss-segment-hosting`：增加 OSS 模式下 `initUrl` / `firstSegUrl` 的双形态语义。

服务在所有 API 响应（`EpisodeInfo` 中 `playUrl` / `coverUrl` / `fallback.low` / `fallback.high` / `drm.keyUri`；`DramaSummary.posterUrl`）和 m3u8 里的 `#EXT-X-KEY:URI` 中 MUST 使用 host-relative 相对路径（形如 `/videos/{slug}/ep-{n}/...` 或 `/drm/{slug}/ep-{n}/key`），不允许包含 scheme / host。

`initUrl` / `firstSegUrl` 字段语义随 OSS 模式切换：OSS 模式启用时 MUST 是以 `oss_upload.oss_public_base_url` 开头的绝对 URL；OSS 模式未启用时 MUST 是相对路径（与 `playUrl` 同形态）。两种模式下该字段都 MUST 满足 `episode-info-schema.json` 的 `uri-reference` 校验。

`drm.keyUri` 与 m3u8 里的 `#EXT-X-KEY:URI` MUST 字节级一致（verbatim 契约不变；与 OSS 模式无关）。服务 SHALL NOT 依赖 `PUBLIC_BASE_URL` 环境变量；该变量在 sdk-drama-listing change 中已废弃。

#### Scenario: 相对类 URL 字段以 / 开头不含 scheme
- **GIVEN** 一条 `status=ready` 的剧集记录（OSS 模式启用或未启用均可）
- **WHEN** 客户端请求 `GET /api/episodes/{slug}/{ep}`
- **THEN** `playUrl` / `coverUrl` / `fallback.low` / `fallback.high` / `drm.keyUri` 每个字段 MUST 以 `/` 开头、MUST NOT 以 `//` 开头、MUST NOT 包含 `://`

#### Scenario: drm.keyUri 与 m3u8 #EXT-X-KEY:URI verbatim 一致
- **GIVEN** 一条 `status=ready` 的剧集记录（OSS 模式启用或未启用均可）
- **WHEN** 客户端分别获取 `drm.keyUri` 和 m3u8 里 `#EXT-X-KEY:URI=...` 的字符串
- **THEN** 两者字节级相等

#### Scenario: initUrl / firstSegUrl 形态由 OSS 模式决定
- **GIVEN** OSS 模式启用
- **WHEN** 客户端请求 `GET /api/episodes/{slug}/{ep}`
- **THEN** `initUrl` 和 `firstSegUrl` MUST 以 `oss_upload.oss_public_base_url` 开头
- **GIVEN** OSS 模式未启用
- **WHEN** 客户端请求 `GET /api/episodes/{slug}/{ep}`
- **THEN** `initUrl` 和 `firstSegUrl` MUST 以 `/videos/` 开头

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
