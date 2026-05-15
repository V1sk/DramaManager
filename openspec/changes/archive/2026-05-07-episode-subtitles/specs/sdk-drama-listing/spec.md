## MODIFIED Requirements

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
