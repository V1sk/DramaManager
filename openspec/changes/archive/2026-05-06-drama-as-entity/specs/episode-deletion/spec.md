## MODIFIED Requirements

### Requirement: 单集删除端点

服务 SHALL 提供 `DELETE /admin/episodes/{drama_slug}/{ep}` 端点。`drama_slug` 必须匹配 `^[a-z0-9][a-z0-9-]*$`，`ep` 必须匹配 `^[0-9]+$`，否则 422。

当匹配的行存在且 `status != 'encoding'` 时，服务 SHALL 依次执行：
1. 从 `episodes` 表删除该行。
2. 删除 `out/{drama_slug}/ep-{ep}/` 整个目录（`shutil.rmtree`，容忍目录不存在）。
3. 删除 `out/{drama_slug}/keys/ep-{ep}.key`、`.iv`、`.key.b64` 三个文件（容忍文件不存在）。

服务 SHALL NOT 删除 `out/{drama_slug}/` 整个目录，即使删除后该剧 DB 中已无任何 episode 行。剧目录的清理归 `drama-entity` capability 的 `DELETE /admin/dramas/{slug}` 端点负责。

任何磁盘删除失败 MUST NOT 阻塞 DB 删除；失败路径 SHALL 记入 server log (WARNING 级别) 并出现在响应体的 `warnings` 数组中。端点返回 `200 {"ok": true, "warnings": [...]}`。

#### Scenario: 正常删除一集并清理磁盘
- **GIVEN** `dramas` 中存在 `slug='ly'`，`out/ly/ep-3/720p/media-720p.m3u8` 等产物存在、`out/ly/keys/ep-3.key` 等存在、`episodes` 中 `(drama_slug=ly, ep_number=3, status=ready)`；同剧还有 `ep_number=1, 2` ready
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应 200 `{"ok": true, "warnings": []}`
- **AND** `episodes` 中 `(ly, 3)` 行已删除，`(ly, 1)`、`(ly, 2)` 保留
- **AND** `out/ly/ep-3/` 目录已不存在
- **AND** `out/ly/keys/ep-3.key`、`ep-3.iv`、`ep-3.key.b64` 已不存在
- **AND** `out/ly/` 目录仍存在（因为还有 ep-1、ep-2）
- **AND** `dramas` 中 `slug='ly'` 行仍存在

#### Scenario: 删除一部剧的最后一集不清空剧目录
- **GIVEN** `dramas` 中存在 `slug='solo'`，`episodes` 中仅有 `(drama_slug=solo, ep_number=1)`；`out/solo/ep-1/` 和 `out/solo/keys/ep-1.*` 存在
- **WHEN** 客户端请求 `DELETE /admin/episodes/solo/1`
- **THEN** 响应 200
- **AND** `episodes` 中 `(solo, 1)` 行已删除
- **AND** `out/solo/ep-1/` 不存在
- **AND** `out/solo/keys/ep-1.*` 不存在
- **AND** `out/solo/` 目录仍然存在（保留给后续可能的 ep-2 上传，或等 `DELETE /admin/dramas/solo` 显式清理）
- **AND** `dramas` 中 `slug='solo'` 行仍存在
