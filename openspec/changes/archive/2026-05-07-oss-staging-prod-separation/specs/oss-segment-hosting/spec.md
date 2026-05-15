## MODIFIED Requirements

### Requirement: pipeline 后处理上传到 OSS 并改写 m3u8

OSS 模式启用时，worker 在 `pipeline.sh` 三个 stage 全部成功后、`set_status('ready')` 之前，MUST 对每档 ladder（540p / 720p / 1080p）：

1. 把该档目录下 `init-{rung}.mp4` 和所有 `seg-{rung}-*.m4s` 通过 `app.oss_upload.upload_file(oss_path, local_file_path)` 上传到 OSS。`oss_path` MUST 形如 `f"{OSS_STAGING_PREFIX}/{drama_slug}/ep-{n}/{rung}/{filename}"`（即 `Drama/staging/...` 前缀 + 服务自身的目录约定），其中 `OSS_STAGING_PREFIX` 从 `app.oss_upload` import；`oss_path` MUST NOT 以 `/` 开头。
2. `upload_file` 返回字典中 `result == True` 视为成功；`False` 视为失败并 raise，由调用方转化为 episode `status=failed`。
3. 改写本地 `media-{rung}.m3u8` —— `#EXT-X-MAP:URI` 行内层 URI 与 segment 行 MUST 替换为对应的绝对 OSS URL，前缀 = `f"{oss_staging_public_base_url}/{drama_slug}/ep-{n}/{rung}/"`；`#EXT-X-KEY:URI` 行 MUST 不变。

任一档的上传或改写失败时，worker MUST `set_status('failed', error_message=...)` 并保留本地产物供事后排查；MUST NOT 把 episode 状态置为 `ready`。

prod 前缀 (`Drama/prod/...`) MUST NOT 由 worker 直接写入；prod 对象只能通过 `app.publish.publish_ladder_to_prod` 在 sync 时刻产生（详见 `oss-staging-prod-separation` capability）。

#### Scenario: 三档全部成功后 episode 进入 ready
- **GIVEN** OSS 模式启用，worker 已完成 pipeline 三个 stage
- **WHEN** 三档 ladder 的 init.mp4 + 全部 .m4s 都成功上传到 staging 前缀，三个 m3u8 改写完成
- **THEN** DB 中该 episode 的 `status` 等于 `ready`
- **AND** 本地 `out/{slug}/ep-{n}/{rung}/media-{rung}.m3u8` 中 `#EXT-X-MAP:URI` 与 segment 行 MUST 是以 `oss_staging_public_base_url` 开头的绝对 URL（即 `https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/...`）
- **AND** 同一 m3u8 中 `#EXT-X-KEY:URI` MUST 仍是 `/drm/{slug}/ep-{n}/key` 相对路径
- **AND** OSS 中 `Drama/prod/{slug}/...` 路径下 MUST 没有由 worker 创建的对象

#### Scenario: 任一上传失败 → episode 进入 failed
- **GIVEN** OSS 模式启用，worker 完成 pipeline 三个 stage
- **WHEN** 上传 720p 档某 segment 时 `upload_file` 返回 `{'result': False, ...}` 或抛出异常
- **THEN** DB 中该 episode 的 `status` 等于 `failed`
- **AND** `error_message` 字段 MUST 包含失败档位（`720p`）和失败原因摘要
- **AND** 本地 `out/{slug}/ep-{n}/` 目录与 keys 文件 MUST 保留，未被清理
- **AND** 已上传到 staging 的对象 MAY 残留（清理由人工 / `unpublish_episode_from_staging` 处理）

#### Scenario: oss_path 形态符合 staging 前缀约定
- **GIVEN** OSS 模式启用，正在上传 `drama_slug=zhetian, ep_number=1, ladder=720p` 的第 0 个切片
- **WHEN** 调用 `upload_file(oss_path, local_file_path)`
- **THEN** `oss_path == "Drama/staging/zhetian/ep-1/720p/seg-720p-0.m4s"`
- **AND** `oss_path` MUST NOT 以 `/` 开头
- **AND** 同档对应 init 的上传调用使用 `oss_path == "Drama/staging/zhetian/ep-1/720p/init-720p.mp4"`

### Requirement: EpisodeInfo URL 字段双形态

`_row_to_episode_info` MUST 根据 `settings.oss_enabled` 切换 `initUrl` / `firstSegUrl` 的拼接方式：

- OSS 模式启用时：`initUrl` = `f"{oss_staging_public_base_url}/{drama_slug}/ep-{n}/720p/init-720p.mp4"`，`firstSegUrl` = `f"{oss_staging_public_base_url}/{drama_slug}/ep-{n}/720p/seg-720p-0.m4s"`。
- OSS 模式未启用时：保持现行相对路径 `/videos/{drama_slug}/ep-{n}/720p/init-720p.mp4` 与 `/videos/{drama_slug}/ep-{n}/720p/seg-720p-0.m4s`。

`playUrl` / `fallback.low` / `fallback.high` / `coverUrl` / `drm.keyUri` MUST 永远是相对路径，与 OSS 模式开关无关。

`_row_to_episode_info` MUST NOT 引用 `oss_prod_public_base_url`；prod 形态的 URL 由业务服务器（step 6）独立构造。

#### Scenario: OSS 启用时 initUrl 是 staging 绝对 URL
- **GIVEN** `OSS_ENABLED=true`，DB 中存在 `drama_slug=ly, ep_number=3, status=ready` 的行
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** `initUrl == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/ly/ep-3/720p/init-720p.mp4"`
- **AND** `firstSegUrl == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging/ly/ep-3/720p/seg-720p-0.m4s"`
- **AND** `playUrl == "/videos/ly/ep-3/720p/media-720p.m3u8"`
- **AND** `fallback.low == "/videos/ly/ep-3/540p/media-540p.m3u8"`
- **AND** `drm.keyUri == "/drm/ly/ep-3/key"`
- **AND** `coverUrl == "/videos/ly/ep-3/cover.jpg"`

#### Scenario: OSS 未启用时所有 URL 相对路径
- **GIVEN** 启动时未设 `OSS_ENABLED`，DB 中存在 `drama_slug=ly, ep_number=3, status=ready` 的行
- **WHEN** 客户端请求 `GET /api/episodes/ly/3`
- **THEN** `initUrl` / `firstSegUrl` MUST 以 `/` 开头、MUST NOT 包含 `://`
- **AND** 整个响应可通过 `episode-info-schema.json` 校验

#### Scenario: drm.keyUri 与 m3u8 EXT-X-KEY URI 永远 verbatim 一致
- **GIVEN** OSS 模式启用，一条 `status=ready` 的剧集记录
- **WHEN** 客户端取 `EpisodeInfo.drm.keyUri` 与对应 m3u8 里 `#EXT-X-KEY:URI="..."` 的内层字符串
- **THEN** 两者字节级相等
- **AND** 该字符串 MUST 是相对路径形如 `/drm/{slug}/ep-{n}/key`

### Requirement: `app/oss_upload.py` 暴露公网 base URL 常量

`app/oss_upload.py` MUST 在模块顶部导出常量 `oss_public_base_url`、`oss_staging_public_base_url`、`oss_prod_public_base_url`：

- `oss_public_base_url`：bucket-level base，等于 `f"https://{bucket_name}.{endpoint_host}/{ossBaseDir}"`（当前为 `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"`）。**仅供 `oss_upload.py` 内部派生 staging / prod 子常量使用。**
- `oss_staging_public_base_url`：`f"{oss_public_base_url}/staging"`（当前为 `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging"`）。
- `oss_prod_public_base_url`：`f"{oss_public_base_url}/prod"`（当前为 `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod"`）。

`app.publish` 与 `app.routers.api` 在拼装绝对 OSS URL 时 MUST 引用 `oss_staging_public_base_url`（这是 staging 服务器，对外只发 staging 形态 URL）；`oss_prod_public_base_url` 仅在 `app.publish.publish_ladder_to_prod` 内部使用，用于把 m3u8 文本中的 staging URL 替换为 prod URL。

`oss_public_base_url` 不再被 `publish.py` 或 `routers/api.py` 直接引用；只在 `oss_upload.py` 内部用于派生上述两个 env-specific 常量。

#### Scenario: 三个常量都暴露且其它模块按角色 import
- **GIVEN** `app/oss_upload.py`
- **WHEN** 加载该模块
- **THEN** `app.oss_upload.oss_public_base_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"`
- **AND** `app.oss_upload.oss_staging_public_base_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/staging"`
- **AND** `app.oss_upload.oss_prod_public_base_url == "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod"`
- **AND** `app/publish.py` 与 `app/routers/api.py` 中拼装绝对 OSS URL 处 MUST 引用 `oss_staging_public_base_url`，MUST NOT 直接引用 `oss_public_base_url`
