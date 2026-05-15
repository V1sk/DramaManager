# oss-segment-hosting

双 host 拓扑：业务 host 服务 m3u8 / key / cover；OSS 服务 init + 加密 .m4s。归档自 `2026-04-30-oss-segment-hosting`。

> 该 change 还修订了 sdk-drama-listing 里的 "API 响应与 m3u8 使用 host-relative URL" 要求（增加 OSS 模式下 `initUrl` / `firstSegUrl` 双形态语义）；修订后的版本归在 `openspec/specs/sdk-drama-listing/spec.md` 内，本 spec 只保留新增条目。

## Requirements

### Requirement: OSS 模式由单一 `OSS_ENABLED` 开关控制

服务的 `Settings` MUST 暴露一个布尔字段 `oss_enabled`，从环境变量 `OSS_ENABLED` 读取（取值 `true` / `1` / `yes`，大小写不敏感视为启用；缺省或其它值视为未启用）。OSS 凭证、endpoint、bucket、`ossBaseDir`、公网 base URL 全部硬编码在 `app/oss_upload.py`，MUST NOT 进入 `Settings`、MUST NOT 通过 env 配置。

#### Scenario: OSS_ENABLED 未设 → 单 host 模式启动
- **GIVEN** 启动环境中没有 `OSS_ENABLED` 变量
- **WHEN** 服务启动
- **THEN** 启动成功
- **AND** `settings.oss_enabled` 为 `False`
- **AND** 后续 `EpisodeInfo.initUrl` / `firstSegUrl` MUST 是相对路径（`/videos/...`）
- **AND** worker 在 pipeline 完成后 MUST NOT 调用 `oss_upload.upload_file`

#### Scenario: OSS_ENABLED=true → OSS 模式启动
- **GIVEN** 启动环境中 `OSS_ENABLED=true`
- **WHEN** 服务启动
- **THEN** 启动成功
- **AND** `settings.oss_enabled` 为 `True`
- **AND** 后续 `EpisodeInfo.initUrl` / `firstSegUrl` MUST 是绝对 URL（以 `oss_upload.oss_public_base_url` 开头）
- **AND** worker 在 pipeline 完成后 MUST 上传切片到 OSS 并改写 m3u8

#### Scenario: OSS_ENABLED=false / 未识别值 → 单 host 模式
- **GIVEN** 启动环境中 `OSS_ENABLED=false` 或 `OSS_ENABLED=maybe`
- **WHEN** 服务启动
- **THEN** `settings.oss_enabled` 为 `False`
- **AND** 行为同 "OSS_ENABLED 未设" 场景

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

### Requirement: m3u8 改写规则

`rewrite_playlist(text, oss_base)` 函数处理 m3u8 行时 MUST 遵循以下规则：

| 行模式 | 动作 |
|---|---|
| `^#EXT-X-MAP:URI="..."` | 把内层 URI 替换为 `{oss_base}/{原文件名}`，整行保留前后属性顺序 |
| `^#EXT-X-KEY:` | 透传不变 |
| `^#` 其它（`#EXTM3U`、`#EXT-X-VERSION`、`#EXTINF` 等） | 透传不变 |
| 空行 | 透传不变 |
| 其它（视为 segment 行） | 整行替换为 `{oss_base}/{原行内容}`，保留行尾换行 |

`oss_base` 由调用方拼好（含 ladder 段），形如 `https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p`。

#### Scenario: EXT-X-MAP 行内层 URI 被替换
- **GIVEN** 原始 m3u8 包含 `#EXT-X-MAP:URI="init-720p.mp4"`
- **WHEN** 调用 `rewrite_playlist(text, "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p")`
- **THEN** 输出对应行为 `#EXT-X-MAP:URI="https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p/init-720p.mp4"`

#### Scenario: EXT-X-KEY 行不被改动
- **GIVEN** 原始 m3u8 包含 `#EXT-X-KEY:METHOD=AES-128,URI="/drm/zhetian/ep-1/key",IV=0xabcd...`
- **WHEN** 调用 `rewrite_playlist(text, "https://photobundle...")`
- **THEN** 输出该行 MUST 与输入逐字节相等

#### Scenario: segment 行被前缀
- **GIVEN** 原始 m3u8 segment 行为 `seg-720p-3.m4s`
- **WHEN** 调用 `rewrite_playlist(text, "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p")`
- **THEN** 输出对应行为 `https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/zhetian/ep-1/720p/seg-720p-3.m4s`

#### Scenario: 元数据行透传
- **GIVEN** 原始 m3u8 包含 `#EXTM3U`、`#EXT-X-VERSION:7`、`#EXT-X-TARGETDURATION:2`、`#EXT-X-PLAYLIST-TYPE:VOD`、`#EXTINF:2.000000,`、`#EXT-X-ENDLIST`
- **WHEN** 调用 `rewrite_playlist(text, "https://...")`
- **THEN** 这些行 MUST 与输入逐字节相等

#### Scenario: 已改写的 m3u8 再次改写是 no-op
- **GIVEN** 一段已经过 rewrite 的 m3u8（segment 行已是绝对 URL，`#EXT-X-MAP` 内层 URI 也已是绝对 URL）
- **WHEN** 用同一个 `oss_base` 再次调用 `rewrite_playlist`
- **THEN** 输出与输入逐字节相等（保证迁移脚本幂等）

### Requirement: EpisodeInfo URL 字段双形态

`_row_to_episode_info` MUST 根据 `settings.oss_enabled` 切换 `initUrl` / `firstSegUrl` 的拼接方式：

- OSS 模式启用时：`initUrl` = `f"{oss_staging_public_base_url}/{drama_slug}/ep-{n}/720p/init-720p.mp4"`，`firstSegUrl` = `f"{oss_staging_public_base_url}/{drama_slug}/ep-{n}/720p/seg-720p-0.m4s"`。
- OSS 模式未启用时：保持现行相对路径 `/videos/{drama_slug}/ep-{n}/720p/init-720p.mp4` 与 `/videos/{drama_slug}/ep-{n}/720p/seg-720p-0.m4s`。

`playUrl` / `fallback.low` / `fallback.high` / `coverUrl` / `drm.keyUri` MUST 永远是相对路径，与 OSS 模式开关无关。

`_row_to_episode_info` MUST NOT 引用 `oss_prod_public_base_url`；prod 形态的 URL 由业务服务器（business-server-sync capability）独立构造。

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
- **AND** 整个响应可通过 `episode-info-schema.json` 校验（`uri-reference` format）

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
