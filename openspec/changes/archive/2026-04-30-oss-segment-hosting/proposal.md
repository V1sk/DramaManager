## Why

业务规模扩大后，把 init.mp4 / .m4s 切片留在业务服务器（API host）开始不划算：

- 切片是冷热分明的大体量静态文件（一部短剧 20 集 × 3 档 ≈ 几百到上千个 .m4s），更适合放 OSS / S3 这种"按容量计费 + 边缘加速 / CDN"的对象存储。
- 业务 host 的带宽 / 出口流量也不应该承担长尾视频回源压力。

但 DRM key（`/drm/{slug}/ep-{n}/key`）必须留在业务 host —— 它是有访问控制语义的内容（即便 MVP 没鉴权，未来一定会加），且 SDK 的 DrmKeyStore fast-start 依赖与 m3u8 里 `#EXT-X-KEY:URI` 的 verbatim 字节级匹配，跨 host 化等于丢掉这层快路径。

m3u8 也最适合留在业务 host：它本身体量小、读取频次低、需要承担"对外的播放入口"角色，且与 key 同 host 才能让 `#EXT-X-KEY:URI` 用相对路径跨剧集复用一套规则；播放器对 m3u8 里的相对 URI 默认按 m3u8 自身 host 解析，相对路径只有在"key host = m3u8 host"时才能正确解析到业务服务器。

由此得到双 host 拓扑：

| 资源 | host | URI 写法 |
|---|---|---|
| `media-{rung}.m3u8` | 业务 host | API 响应 `playUrl` / `fallback.*` 用相对路径 |
| `#EXT-X-KEY:URI` | 业务 host（同 m3u8） | 相对路径 `/drm/...`，由播放器拼 m3u8 host |
| `init-{rung}.mp4` | OSS host | m3u8 里写**绝对 OSS URL**；`EpisodeInfo.initUrl` 同 |
| `seg-{rung}-N.m4s` | OSS host | m3u8 里写**绝对 OSS URL**；`EpisodeInfo.firstSegUrl` 同 |
| `cover.jpg` | 业务 host | 相对路径（量小、频次低、可被替换 endpoint 改写，留 API 简单） |
| `*.key` / `*.iv` / `*.key.b64` | 业务 host（绝不上 OSS） | 不走 URL 暴露 |

如果不做这个 change，现状（所有产物都在 API host）会继续，但带宽 / 存储成本随剧目规模线性增长。

## What Changes

**复用 `app/oss_upload.py`**：该模块已封装好 `upload_file(oss_path, local_file_path) -> dict` 与 `ossBaseDir = 'Drama'` 前缀常量，内部使用 `oss2.Bucket` + 阿里云 OSS endpoint `oss-ap-southeast-1.aliyuncs.com` + bucket `photobundle`。本 change **直接调用此函数完成上传**，不再引入 `boto3` 依赖、不再设计可插拔 `Uploader` 抽象层 —— 单一存储后端 + 凭证已硬编码 + 单进程足够，过早抽象只增维护面。

**新增"发布"后处理**：pipeline 三个 stage 跑完以后，worker 把每档 ladder 下的 `init-{rung}.mp4` + 加密后的 `seg-{rung}-*.m4s` 通过 `upload_file` 推到 OSS，然后**改写本地 `media-{rung}.m3u8`**，把这两类引用替换成绝对 OSS URL；`#EXT-X-KEY:URI` 行**保持原样不动**（仍是 `/drm/{slug}/ep-{n}/key` 相对路径）。改写后的 m3u8 落在 `OUT_DIR/{slug}/ep-{n}/{rung}/` 原位置，由现有 `/videos` 静态挂载继续对外提供。

**OSS object key 的拼接**：调用 `upload_file` 前，调用方 MUST 把 `ossBaseDir` 拼到前面，按本服务自身的目录规则形成最终 key。例如 720p 第 0 个切片：

```
oss_path      = f"{ossBaseDir}/{slug}/ep-{n}/720p/seg-720p-0.m4s"
                # 实际值：'Drama/zhetian/ep-1/720p/seg-720p-0.m4s'
local_file    = OUT_DIR / slug / f"ep-{n}" / "720p" / "seg-720p-0.m4s"
upload_file(oss_path, str(local_file))
```

**新增配置项（`app/config.py::Settings`）**：唯一一个 OSS 相关 env：

| env | 必填 | 默认 | 用途 |
|---|---|---|---|
| `OSS_ENABLED` | 否 | `false` | `true` / `1` 启用上传链路；`false` 时单 host 模式（行为同今日） |

OSS 凭证 / endpoint / bucket / `ossBaseDir` 全部硬编码在 `app/oss_upload.py`，不进 env，不进 `Settings`。客户端可见的"OSS 公网前缀"由 `oss_upload.py` 模块的 `endpoint` + bucket 名 + `ossBaseDir` 派生：在 `app/oss_upload.py` 里**新增**模块级常量 `oss_public_base_url`，值为 `"https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"`（即 `https://{bucket}.{endpoint_host}/{ossBaseDir}`），由本 change 一并补上；其它代码统一从这里 import，避免在 publish / api 里再硬编码一次。

**EpisodeInfo URL 字段语义微调**：

| 字段 | OSS 启用时 | OSS 未启用时 |
|---|---|---|
| `playUrl` | 相对路径（业务 host） | 相对路径（同今） |
| `fallback.low` / `.high` | 相对路径（业务 host） | 相对路径（同今） |
| `drm.keyUri` | 相对路径（业务 host） | 相对路径（同今） |
| `coverUrl` | 相对路径（业务 host） | 相对路径（同今） |
| `initUrl` | **绝对 OSS URL** | 相对路径（同今） |
| `firstSegUrl` | **绝对 OSS URL** | 相对路径（同今） |

`episode-info-schema.json` 里这些字段早已是 `format: uri-reference`（sdk-drama-listing D7 已统一），相对 / 绝对都合法，**不动 schema**。

**m3u8 改写**：在 `app/publish.py` 新增纯函数 `rewrite_playlist(text: str, oss_base: str) -> str`，逐行处理：

- `^#EXT-X-MAP:URI="..."` —— 用绝对 OSS URL 替换内层 URI
- 形如 `seg-{rung}-N.m4s` 的非注释行 —— 整行替换为绝对 OSS URL
- `^#EXT-X-KEY:` —— 透传，**不改**
- 其它 `#EXT-...` 元数据行、空行 —— 透传

**worker 流程编排（`app/queue.py::_handle_job`）**：pipeline 成功后，如果 `settings.oss_enabled`：按 ladder 循环（540p / 720p / 1080p）调 `publish_ladder`，三档全部成功才 `set_status(ready, ...)`。任一失败 → `set_status(failed, error_message=...)`，本地产物保留供事后分析（与现有 pipeline 失败语义一致）。`upload_file` 是同步阻塞调用，worker 协程里走 `asyncio.to_thread(...)` 包一下避免阻塞事件循环。

**保留本地产物**：上传成功后**不删除**本地的 init.mp4 / seg.m4s，留作灾难恢复 / 调试 / `DELETE /admin/episodes/...` 时直接 `rm -rf` 的来源。OSS 端的回收**不在本 change 范围**（`app/oss_upload.py` 当前只提供 `upload_file`，没有 `delete_*` 入口）—— 删除一集时 OSS 上对应前缀的对象会成为孤儿对象，需要后续 follow-up（添加 `delete_prefix` helper + episode-deletion handler 联动）。

**不改**：

- DB schema、`episode-info-schema.json`、pipeline 三个 stage 脚本、SDK 端点形状、管理页 UI、m3u8 里 `#EXT-X-KEY:URI` 的写法、相对路径 + `keyUri` verbatim 契约。
- `pipeline.sh` / `encode-clear.sh` / `encrypt-segments.sh` 一行不动 —— 它们负责"产出本地正确的密文产物"，OSS 是部署 / 发布层的事。
- `app/oss_upload.py` 的现有 API（`upload_file`、`ossBaseDir`、`endpoint`、`bucket` 对象）形状不动；只新增一个常量 `oss_public_base_url`。

## Capabilities

### New Capabilities

- `oss-segment-hosting`: 双 host（业务 host 服务 m3u8 + key + cover；OSS 服务 init + 加密切片）的发布与改写流程。覆盖：上传策略（复用 `oss_upload.upload_file` + `ossBaseDir` 前缀）、m3u8 改写规则、`#EXT-X-KEY:URI` 不变契约、EpisodeInfo URL 语义（哪些字段切换为绝对 OSS URL）、双 host 与单 host 配置形态的并存（`OSS_ENABLED=false` 回退到单 host）、上传失败时的 episode 状态机。

### Modified Capabilities

- `sdk-drama-listing`：`Requirement: API 响应与 m3u8 使用 host-relative URL` 的措辞需要细化 —— 不再"全部相对"，而是分两类：m3u8 / key / cover / playUrl / fallback / drm.keyUri 仍相对；initUrl / firstSegUrl 在 OSS 启用时为绝对 URL；`drm.keyUri` 与 m3u8 里 `#EXT-X-KEY:URI` 的 verbatim 契约保持。

## Impact

- **新增代码**：
  - `app/config.py`：新增 `oss_enabled: bool` 字段（从 env 读 `OSS_ENABLED`）。
  - `app/oss_upload.py`：新增模块级常量 `oss_public_base_url = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"`（其它代码 import 这个常量拼绝对 URL）。
  - `app/publish.py`：`rewrite_playlist()` 纯函数 + `publish_ladder(slug, ep_dir, ladder)` 编排函数（遍历 ladder 目录调 `oss_upload.upload_file`，再改写 m3u8 写回原位）。
  - `app/queue.py::_handle_job`：pipeline 成功后，`if settings.oss_enabled` 时调 `publish_ladder` 三次，任一档失败 → status=failed。
  - `app/routers/api.py::_row_to_episode_info`：根据 `settings.oss_enabled` 决定 `initUrl` / `firstSegUrl` 走相对（`/videos/...`）or 绝对（`oss_public_base_url + ...`）。
- **不动**：`pipeline.sh` / `encode-clear.sh` / `encrypt-segments.sh` / `episode-info-schema.json` / DB schema / SDK 端点形状 / 管理页 HTML / `app/oss_upload.py` 既有 API。
- **文档**：`CLAUDE.md`：env 表新增 1 行（`OSS_ENABLED`）；URL map 注明 `initUrl` / `firstSegUrl` 在 OSS 模式下是 OSS 绝对 URL；新增段落"OSS 双 host 拓扑"解释 m3u8 / key / 切片三者的 host 归属、CORS 注意点。
- **依赖**：无新增 —— `oss2` 已被 `app/oss_upload.py` 引入。
- **对已部署实例**：
  - 不设置 `OSS_ENABLED=true` 重启 → 行为完全等同今日，纯增量代码不影响老路径。
  - 设置 `OSS_ENABLED=true` 后老数据：DB 行的 `play_url` / `key_uri` 仍相对路径，仍可用；但 `initUrl` / `firstSegUrl` 由 helper 实时拼出，对老剧集会拼出"OSS 上其实没传过的"绝对 URL（404）。**Mitigation**：附迁移脚本 `scripts/migrate_to_oss.py`，扫描 `status=ready` 行 → 上传本地切片 → 改写 m3u8。或者干脆重传。

## Out of Scope（Follow-ups）

- **OSS 端删除联动**：`app/oss_upload.py` 当前只提供 `upload_file`。`DELETE /admin/episodes/...` 时 OSS 上对应前缀对象会成为孤儿。后续在 `oss_upload.py` 添加 `delete_prefix(oss_prefix)` helper + 在 episode-deletion handler 末尾调用 + 失败 `warnings: ["oss cleanup partial: ..."]` 降级。
- **OSS 私有桶 + 签名 URL**：本 change 假设桶公开读。等"加鉴权"那波 change 一起做。
- **CDN 加速 / 自定义域名**：换成 CDN 域名时，把 `oss_upload.oss_public_base_url` 改成 CDN 前缀即可，无代码改动。
- **`POST /admin/episodes/.../republish`**：单独重跑发布（不重转码）。短期不加，等运营反馈。
