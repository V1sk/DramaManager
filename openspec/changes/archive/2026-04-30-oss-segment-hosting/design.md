## Context

`sdk-drama-listing` change 把所有 URL 字段统一为 host-relative，前提是"所有静态资源都在同一个 API host"。一旦把 init.mp4 / .m4s 切片挪到 OSS（不同 host），相对路径会被播放器按 m3u8 自身 host 解析，而 m3u8 留在业务 host —— 那播放器会去业务 host 拉一条不存在的 segment URL，404。

但 DRM key 又不能跟着切片上 OSS：

1. key 是访问控制点（即便 MVP 没鉴权，未来一定要加 token / 鉴权头），放 OSS 等于把"鉴权 + 计费"语义灌到对象存储 ACL 体系里，复杂度爆炸。
2. SDK 的 DrmKeyStore fast-start 把 `#EXT-X-KEY:URI` 字符串当作 lookup key，从 `EpisodeInfo.drm.keyUri` 预填 key bytes —— 两个字符串必须 verbatim 相等。如果 m3u8 里写绝对 OSS URL（错），SDK 主动调 keyUri 是业务 host 相对路径（对），两边对不上，fast-start 失效。

所以解法是**双 host 拓扑**：m3u8 + key + cover 留业务 host，init + .m4s 上 OSS。m3u8 里：

- `#EXT-X-MAP:URI=` / segment 行 → **绝对 OSS URL**（绕过相对路径解析，直达 OSS）
- `#EXT-X-KEY:URI=` → **保持相对路径**（播放器按 m3u8 host = 业务 host 解析，命中 `/drm/...`）

这是 HLS spec 允许的 —— 同一个 m3u8 内绝对 URL 和相对 URL 可以混用，相对 URL 一律按 m3u8 自身的 base URI 解析（RFC 8216 §4）。

OSS 上传层在 `app/oss_upload.py` 里已经由人工准备好：模块导出 `upload_file(oss_path, local_file_path) -> dict`、常量 `ossBaseDir = 'Drama'`，凭证 / endpoint / bucket / SDK（`oss2`）已硬编码。本 change 不动这一层、不引入抽象，直接调用即可。

## Goals / Non-Goals

**Goals**：

- 切片 / init 移到 OSS（阿里云 `oss-ap-southeast-1` 的 `photobundle` 桶 + `Drama/` 前缀）；m3u8 / key / cover 留业务 host。
- 不改 pipeline 三个 stage 脚本 —— 它们的职责是产出"本地正确的密文产物"。
- DRM fast-start 契约不破：`drm.keyUri` 与 m3u8 里 `#EXT-X-KEY:URI` 仍然 verbatim 一致。
- 单 host 部署形态保留 —— `OSS_ENABLED=false`（默认）时行为等同今日，不强制所有部署上 OSS。
- 复用 `app/oss_upload.py` 既有 API，不另起轮子、不引入 boto3。

**Non-Goals**：

- 不做 OSS / CDN 鉴权（签名 URL、防盗链）—— MVP 假设 OSS 桶公开读。需要鉴权时另开 change。
- 不做 OSS 多桶 / 多区域 / 按剧路由 —— 单桶 + 固定 `Drama/` 前缀够用。
- 不做"上传到 OSS 后删本地"的存储优化 —— 内部系统规模下盘价便宜，本地副本利于调试 / 灾难恢复。
- 不替换现有 `/videos` 静态挂载 —— 本地副本仍能通过 `/videos/...` 访问，只是新部署的客户端不再走这条路。
- 不改 `pipeline.sh` / `encode-clear.sh` / `encrypt-segments.sh`。
- 不在本 change 里做 OSS 端的删除联动 —— `oss_upload.py` 当前没有 `delete_prefix`，episode-deletion 删除时 OSS 上对应前缀对象会成为孤儿；follow-up 处理。

## Decisions

### D1. 改写 m3u8 的位置：worker 后处理，不动 stage 脚本

**决策**：在 `app/queue.py::_handle_job` 里 pipeline 三个 stage 跑完之后调一个新模块 `app/publish.py::publish_ladder(slug, ep_dir, ladder)` 完成"上传切片 + 改写 m3u8"。stage 脚本一行不动。

**理由**：

- pipeline 脚本的语义是"在本地产出正确的密文 CMAF 产物 + m3u8" —— 这是个干净的边界，shell 脚本不该懂 OSS 配置 / SDK / 端点 URL 这一类发布概念。
- worker 已经是"协调本地产物 + DB 状态 + 错误回写"的中枢，发布动作天然属于这一层；放这里复用现成的 status 机器和异常路径。
- Python 改写 m3u8 比在 bash 里 sed 安全得多 —— 字符串里有引号、可能的特殊字符，Python 行级处理 + 显式分支更可控。

**替代方案**：

- 在 `encode-clear.sh` 用 `-hls_base_url ${OSS_PREFIX}/` 让 ffmpeg 直接写绝对 URL。**弃**：(a) 老版本 ffmpeg 的 `-hls_base_url` 不影响 `#EXT-X-MAP:URI`，要补 sed 才完整；(b) stage 脚本被迫吸收发布层配置；(c) 顺序耦合更脆。
- 加 stage 3 shell 脚本 `publish.sh`：可行但要把 oss2 SDK 调用塞进 shell，麻烦。Python 模块内聚更高。

### D2. 直接调用 `oss_upload.upload_file`，不再做 storage 抽象层

**决策**：`app/publish.py::publish_ladder` 里直接 `from .oss_upload import upload_file, ossBaseDir, oss_public_base_url`，对每个待传文件调一次 `upload_file(oss_path, str(local_path))`，把返回字典里 `result == False` 视为失败抛 `PublishError`。不引入 `Uploader` Protocol、不引入 `S3Uploader` 实现、不引入 boto3。

**理由**：

- 业务约束清晰：单一存储后端（阿里云 OSS）、凭证已硬编码、单进程串行调用。提前抽出 Protocol 是 YAGNI —— 真要换后端时改一处 import + 写一个新 wrapper 函数即可，几分钟工作量。
- `oss_upload.py` 已是稳定 API，调用方薄即可。
- 减少需要维护的中间层（`app/storage.py` 不存在）。

**代价**：

- 测试时无法用 mock object 替换 storage 实现，必须 monkey-patch `oss_upload.upload_file` 或拦截 oss2 client。可接受（pytest 用 `monkeypatch.setattr` 一行搞定）。
- 未来真要换非 OSS 后端时需要重构。但那一刻才有真实需求驱动，比现在猜测更靠谱。

**替代方案**：

- 保留 `Uploader` Protocol：典型的过度抽象，否定。
- 在 `oss_upload.py` 里再加一层 wrapper class：增加一层间接，无收益。

### D3. m3u8 改写规则：行级显式判定，不依赖 ffmpeg 输出顺序

**决策**：`rewrite_playlist(text: str, oss_base: str) -> str` 逐行判定：

```
# 行类型           动作
^#EXT-X-MAP:       提取 URI="..."，把内层 URI 拼成绝对 OSS URL，整行重写
^#EXT-X-KEY:       透传（不动；URI 已是相对 /drm/... 业务 host）
^#                 透传（其它 #EXT-* 元数据、注释）
空行                透传
其它               视作 segment 行；整行替换为绝对 OSS URL（保留 trailing newline）
```

`oss_base` 形如 `https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/{slug}/ep-{n}/{rung}`（已含 ladder 段，调用方拼好），改写时只把"文件名"前缀上去。

**理由**：

- 显式四类分支比正则一行替换更稳：`#EXT-X-KEY` 行里 URI 字段也写 `URI="..."`，简单的 `URI="..."` 替换会把 key URI 一起改坏。
- 行级处理不依赖 ffmpeg 输出顺序（即便 ffmpeg 改了 `#EXT-X-MAP` 和 `#EXTINF` 的相对位置，逻辑也成立）。
- 函数纯（无 I/O）—— 单测覆盖各种 m3u8 形态零成本。

**替代方案**：

- `awk` 在 bash 里改：违反 D1（stage 脚本不动）。
- 整体 regex 替换：太脆弱（见上）。

### D4. EpisodeInfo URL 字段语义：双形态共存，靠 `settings.oss_enabled` 切换

**决策**：`_row_to_episode_info` 在拼 `initUrl` / `firstSegUrl` 时检查 `settings.oss_enabled`：

```python
from ..oss_upload import oss_public_base_url

if settings.oss_enabled:
    base = f"{oss_public_base_url}/{slug}/{ep_dir}"
else:
    base = f"/videos/{slug}/{ep_dir}"
init_url     = f"{base}/720p/init-720p.mp4"
first_seg_url = f"{base}/720p/seg-720p-0.m4s"
```

`playUrl` / `fallback.*` / `coverUrl` / `drm.keyUri` 永远相对路径（始终走业务 host），不分形态。

**理由**：

- schema 里这些字段已是 `format: uri-reference`（`uri | relative-reference`），相对 / 绝对都合法，零迁移。
- `playUrl` / `fallback.*` 永远指向 m3u8 = 业务 host = 相对路径，无论 OSS 启用与否（D1 决定 m3u8 留业务 host）。
- `drm.keyUri` 永远指向业务 host = 相对路径，verbatim 与 m3u8 里 `#EXT-X-KEY:URI` 一致。
- `coverUrl` 也留业务 host：cover 体量小、有"管理员替换"场景（`POST /api/episodes/.../cover`），跟 OSS 上传链路解耦更简单。

**风险**：

- 一个 EpisodeInfo 里同时出现相对（业务 host）和绝对（OSS host）URL，客户端必须懂得"两类共存"。Android SDK 在 OkHttp 里做 baseUrl 解析时，绝对 URL 不需要 baseUrl 参与，零改动；hls.js 同。
- 如果未来 OSS 域名换了，老的 ready 行 m3u8 里仍写老 OSS 域名 —— 需要重写 m3u8 + 重传切片。**Mitigation**：写迁移脚本（D6）。

**替代方案**：

- 把 OSS base URL 也存进 DB：避免 helper 拼，但每次 OSS 切换要写 DB；当前规模不划算。
- API 永远返回相对路径、由网关层做 URL rewrite：网关需要懂 m3u8 内容拼接规则，放大攻击面。

### D5. 启用条件：单一 `OSS_ENABLED` 开关

**决策**：`Settings` 新增 `oss_enabled: bool`，从 env `OSS_ENABLED` 读取（`true` / `1` / `yes` → True，其它 → False，默认 False）。其余 OSS 相关配置（凭证、endpoint、bucket、`Drama` 前缀、公网 base URL）全部硬编码在 `app/oss_upload.py`，不进 env、不进 `Settings`。

**理由**：

- `app/oss_upload.py` 的凭证策略是"硬编码 + 提交进仓库"（用户已经这么做了）—— 我们不再用 env 重复一次同一份信息。
- 单开关比 sdk-drama-listing 删掉的 `PUBLIC_BASE_URL` 更轻量：默认 false 给本地开发 / 离线 demo 留个安全的回退。
- fail-fast 的语义被简化掉了：因为再没有"半启用"的可能（只有一个布尔），也就没有"五个 env 部分填"的边界 case。

**风险**：

- 凭证泄漏：源码已是公开仓库时，OSS access key 跟着泄漏。**Mitigation**：超出本 change 范围；属于代码安全治理，由 oss_upload.py 的作者决定是否后续抽进 env / vault。
- 不能在不同环境（生产 / staging）配不同桶：当前只有一个桶 `photobundle`。多环境时 follow-up（参考 `oss_upload.py` 的 endpoint / bucket 改成 env-driven）。

**替代方案**：

- 不要 `OSS_ENABLED` 开关，改在调用点直接 `if SHOULD_PUBLISH:`：散落、难以发现。
- 把 `OSS_ENABLED` 改成 `OSS_PUBLIC_BASE_URL`（设了即启用）：跟"凭证已硬编码"的现状错配，多此一举。

### D6. 失败的状态机

**决策**：worker 的状态流：

```
encoding → (pipeline.sh 全 ladder 完成) → (publish_ladder × 3) → ready
                                       ↘ 任一失败 → failed (error_message=具体 stage)
```

publish 阶段失败时 `error_message` 形如 `OSS upload failed for 720p: <oss2/HTTP error tail>` 或 `m3u8 rewrite failed for 1080p: <python exception>`。本地产物**保留不删**（与 pipeline 失败一致）—— 留作排错和"修好后重跑发布"的输入。

**理由**：

- ready 必须意味着客户端能完整播放，含 OSS 上的切片可达。把 publish 失败 = ready 是欺骗 SDK。
- 本地产物保留是当前 pipeline-failure 的策略，扩到 publish-failure 一致。
- 重跑：本 change 不引入"单独重跑发布"的端点。简化路径是删掉这条 episode 后重新上传。后续可加 `POST /admin/episodes/.../republish` 单独走 publish_ladder（小改动），先观察使用频次。

**替代方案**：

- 标记一个独立的中间状态 `published_partial`：状态机变长，客户端要懂；当前规模不需要。
- 上传重试 N 次再 failed：oss2 的 `put_object_from_file` 本身有内部重试；外层再叠 N 次只会拖慢失败时的反馈。

### D7. 迁移：脚本扫库，不强制重传

**决策**：附 `scripts/migrate_to_oss.py`：遍历 `status=ready` 行 → 对每集每档执行 `publish_ladder`（上传切片 + 改写 m3u8）→ DB 不动。运行幂等（重复跑只是覆盖 OSS 对象 + 第二次 rewrite 命中已绝对的 URL，是 noop）。

**理由**：

- 不重新走 pipeline.sh，免去重做加密 + 重算 key（key 不变，IV 不变，密文不变）。
- DB 字段 `play_url` / `key_uri` 已是相对路径，不需要更新。改写后的 m3u8 落回原位 → `_row_to_episode_info` 拼出的 `initUrl` / `firstSegUrl` 也立刻指向 OSS，自洽。
- 脚本失败可重跑，幂等。

**替代方案**：

- 强制操作员重传所有现存内容：浪费、也带风险。
- 加 DB 字段记录"是否已发布到 OSS"：状态机扩大，与"客户端能不能播"无关，去除。

### D8. 在 `app/oss_upload.py` 新增一个常量 `oss_public_base_url`

**决策**：在 `oss_upload.py` 模块顶部追加一行：

```python
oss_public_base_url = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"
```

publish.py 和 routers/api.py 从此处 import，不重复硬编码。

**理由**：

- OSS 公网前缀语义上属于 OSS 配置，跟 endpoint / bucket / `ossBaseDir` 同源；放一起便于未来切 CDN 时一处改全。
- 不放进 `Settings` 因为 D5 已经决定 OSS 凭证 / endpoint 不进 env，公网前缀（同样硬编码）也保持同样口径。
- 唯一对 `oss_upload.py` 的入侵：增加一个 module-level 常量。`upload_file` 函数本身签名不动。

**替代方案**：

- 在 publish.py 里硬编码同样的字符串：DRY 违反。
- 在 `Settings` 里加 `oss_public_base_url` 字段（从 env 读）：跟 D5 的精神不一致。

## Risks / Trade-offs

- **[Risk] 凭证 / 桶名硬编码**：源码里直接看得到 access key + secret + bucket。**Mitigation**：超出本 change 范围（沿用 `oss_upload.py` 既有形态）。后续 follow-up 可把 `oss_upload.py` 改成读 env，本 change 的 `OSS_ENABLED` 开关无须变更。
- **[Risk] 双 host CORS**：m3u8 在业务 host，切片在 OSS host。浏览器（hls.js）发请求时如果 OSS 没设 CORS 头，会被 CORS 拒。**Mitigation**：CLAUDE.md 加一段"OSS 桶必须配置 CORS 允许 GET 来自业务 host 的 Origin"，ExoPlayer / iOS 原生 HLS 不受影响（不走浏览器 fetch）。
- **[Risk] OSS 端孤儿对象**：episode-deletion 删除一集时，OSS 上 `Drama/{slug}/ep-{n}/` 前缀对象不会被清。**Mitigation**：follow-up 加 `delete_prefix` helper + 联动；当前规模可接受少量孤儿（人工清理 / 等 follow-up）。
- **[Risk] 老 ready 行迁移盲区**：D7 脚本要手动跑；运维忘了跑，老剧集 SDK 拿到的 `EpisodeInfo` 里 `initUrl` / `firstSegUrl` 是 OSS 绝对 URL（拼出来的），但 OSS 上没传过 → 404。但 `playUrl` 指向的 m3u8 仍在业务 host，里面是相对路径段名 → 由 `/videos` 静态挂载满足，仍然能放。失败的只有"基于 initUrl / firstSegUrl 的 fast-start 预热路径"。**Mitigation**：迁移脚本写完即跑、CLAUDE.md 写明步骤。
- **[Trade-off] m3u8 上 OSS 也算一种选择**：那样需要把 `#EXT-X-KEY:URI` 写绝对业务 host URL，违反 sdk-drama-listing D7 的相对路径策略。当前规模 m3u8 留 API host 简单。
- **[Trade-off] 本地 + OSS 双副本占盘**：内部规模可接受。要省盘时加一个 `OSS_DELETE_LOCAL_AFTER_PUBLISH=true` 开关；本 change 不开此口子。

## Migration Plan

阶段 1（纯部署）：

1. 升级到含本 change 的服务版本，**不设 `OSS_ENABLED`**（默认 false）。重启后行为完全等同今日；纯增量代码不影响热路径。
2. 确认 `app/oss_upload.py` 里的 endpoint / bucket / 凭证可以连通（手工跑一次 `python -c "from app.oss_upload import upload_file; print(upload_file('Drama/test.txt', '/etc/hosts'))"` 之类）。
3. 配置 OSS 桶的公开读 + CORS（来源 = 业务 host）。
4. 设 `OSS_ENABLED=true` 重启服务。新上传的剧集自动走 publish 流。
5. 跑 `python scripts/migrate_to_oss.py` 把现存 ready 行的切片传上去 + 改写 m3u8。
6. 抽查老剧集：拉 EpisodeInfo → `initUrl` / `firstSegUrl` 是绝对 OSS URL → 实播。
7. 确认运行稳定后（跑一周）可以考虑把 `/videos/{slug}/*/*/seg-*.m4s` 在网关层禁掉（强制走 OSS），本 change 范围不动这块。

阶段 1 失败回滚：把 `OSS_ENABLED` 清空（或设 `false`）+ 重启 → 行为退化回今日。已传上 OSS 的对象不影响（不删；老 m3u8 已经被改写，但本地副本还在，相对路径解析能到），但客户端 SDK 已经下载到含绝对 OSS URL 的 m3u8 → 客户端缓存层负责。**Caveat**：客户端如果做了 m3u8 / EpisodeInfo 持久化缓存，回滚后旧客户端仍走 OSS，桶得保活；用 `If-Modified-Since` 等 HTTP 缓存头 + SDK 强制刷新可缓解。

## Open Questions

- 是否给 admin 加 `POST /admin/episodes/{slug}/{ep}/republish`（单独重跑发布步而不重新转码）？短期不加，等运营反馈。
- OSS 端删除联动（在 `oss_upload.py` 加 `delete_prefix`、改 `DELETE /admin/episodes/...` handler、`warnings` 数组追加 `oss cleanup partial:`）—— follow-up change。
- 是否要在 EpisodeInfo 里也带一个 `cdnHost` 显式字段，方便客户端做"健康度探测 / 失败时降级"？短期不加。
- 凭证从硬编码改成 env / vault：safety hygiene，不影响本 change，独立 follow-up。
