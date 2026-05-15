## ADDED Requirements

### Requirement: 上传时探测视频宽高

服务在 `POST /admin/upload` 处理流程中 MUST 在 `probe_duration_ms` 之外额外调用 `probe_video_dimensions(tmp_path) -> tuple[int, int]`，探测源视频第一条 video stream 的 codec `width` 和 `height`（像素值，非 display dimension）。两个值 MUST 都是正整数。探测失败时服务 MUST 与 `probe_duration_ms` 失败一致：清理临时上传文件 + 返回 HTTP 400 + `detail` 含 `ffprobe failed:` 前缀的错误信息。

#### Scenario: 上传成功后宽高写入 DB
- **GIVEN** 一个合法的 720×1280 mp4 上传到 `POST /admin/upload`
- **WHEN** 上传完成
- **THEN** `episodes` 行的 `width` 列等于 720，`height` 列等于 1280
- **AND** 该行 `status` 进入正常的 pending → encoding 流程

#### Scenario: ffprobe 探测失败拒收
- **GIVEN** 一个破损的视频文件上传到 `POST /admin/upload`
- **WHEN** `probe_video_dimensions` 抛 `FfmpegError`
- **THEN** 响应状态码 400，`detail` 以 `ffprobe failed:` 开头
- **AND** 临时上传文件被删除
- **AND** DB 中没有产生新行

#### Scenario: ffprobe 输出非正整数视为失败
- **GIVEN** ffprobe 返回的 width / height 之一为 0 或负数（理论上不可能，防御性约束）
- **WHEN** `probe_video_dimensions` 解析输出
- **THEN** raise `FfmpegError`
- **AND** handler 转 HTTP 400

### Requirement: DB schema 包含 width / height 列且向后兼容

`episodes` 表 MUST 有 `width INTEGER` 和 `height INTEGER` 两列（均可为 NULL）。`init_db()` 在每次启动时 MUST 检查这两列是否存在，缺失时通过 `ALTER TABLE ... ADD COLUMN` 添加（幂等）。

老行（升级前已 ready）的 `width` / `height` MUST 保持 NULL，不被改动。

#### Scenario: 老库升级后两列存在但老行为 NULL
- **GIVEN** 一个升级前已存在 `episodes` 表的 SQLite 文件，老行的 width / height 列尚不存在
- **WHEN** 服务启动调用 `init_db()`
- **THEN** `episodes` 表有 `width` 和 `height` 两列
- **AND** 升级前已存在的行 `width IS NULL` 且 `height IS NULL`
- **AND** 这些老行的其它字段不变

#### Scenario: 重复启动不报错
- **GIVEN** 已经升级过 schema 的库
- **WHEN** 服务再次重启执行 `init_db()`
- **THEN** 不抛异常
- **AND** 数据零损失

### Requirement: EpisodeInfo 透传 width / height

`EpisodeInfo` MUST 含两个新字段：

- `width: integer | null`，`>=1`
- `height: integer | null`，`>=1`

均不进 `required` 数组。`_row_to_episode_info` MUST 从 row 取 `width` / `height` 原样透传给响应，None / NULL 时返回 JSON `null`。单集端点 `GET /api/episodes/{slug}/{ep}` 与按剧集数列表端点 `GET /api/dramas/{slug}/episodes` MUST 在同一行上返回完全相同的 width / height 值（共用同一 helper）。

`episode-info-schema.json` 同步新增这两个字段，`type: ["integer", "null"]`、`minimum: 1`，描述说明是源视频 codec width / height（非 display dimension）。

#### Scenario: 新上传 ready 行响应含正整数 width / height
- **GIVEN** 一个 720×1280 上传完成且 `status=ready` 的剧集 `drama_slug=ly, ep_number=3`
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** 响应 `width == 720`、`height == 1280`
- **AND** 响应可通过 `episode-info-schema.json` 严格校验

#### Scenario: 老 ready 行响应 width / height 为 null
- **GIVEN** 一个升级前就 `status=ready` 的剧集（DB 行 `width IS NULL` 且 `height IS NULL`）
- **WHEN** 客户端请求 `GET /api/episodes/{slug}/{ep}`
- **THEN** 响应 `width == null`、`height == null`
- **AND** 响应可通过 `episode-info-schema.json` 严格校验
- **AND** 其它字段不变

#### Scenario: 单集与列表端点同一行 width / height 一致
- **GIVEN** 一行 `drama_slug=ly, ep_number=3, status=ready, width=720, height=1280`
- **WHEN** 客户端分别请求 `GET /api/episodes/ly/3` 和 `GET /api/dramas/ly/episodes`
- **THEN** 单集响应中的 `width` / `height` 与列表中 `ep_number=3` 元素的 `width` / `height` 字节级相等
