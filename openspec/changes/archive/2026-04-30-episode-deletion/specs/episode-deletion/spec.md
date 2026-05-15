## ADDED Requirements

### Requirement: 单集删除端点

服务 SHALL 提供 `DELETE /admin/episodes/{drama_slug}/{ep}` 端点。`drama_slug` 必须匹配 `^[a-z0-9][a-z0-9-]*$`，`ep` 必须匹配 `^[0-9]+$`，否则 422。

当匹配的行存在且 `status != 'encoding'` 时，服务 SHALL 依次执行：
1. 从 `episodes` 表删除该行。
2. 删除 `out/{drama_slug}/ep-{ep}/` 整个目录（`shutil.rmtree`，容忍目录不存在）。
3. 删除 `out/{drama_slug}/keys/ep-{ep}.key`、`.iv`、`.key.b64` 三个文件（容忍文件不存在）。
4. 若删除后该剧在 DB 中已无任何行（`count_by_slug(drama_slug) == 0`），删除 `out/{drama_slug}/` 整个目录。

任何磁盘删除失败 MUST NOT 阻塞 DB 删除；失败路径 SHALL 记入 server log (WARNING 级别) 并出现在响应体的 `warnings` 数组中。端点返回 `200 {"ok": true, "warnings": [...]}`。

#### Scenario: 正常删除一集并清理磁盘
- **GIVEN** `out/ly/ep-3/720p/media-720p.m3u8` 等产物存在、`out/ly/keys/ep-3.key` 等存在、DB 中 `(drama_slug=ly, ep_number=3, status=ready)`；同剧还有 `ep_number=1, 2` ready
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应 200 `{"ok": true, "warnings": []}`
- **AND** DB 中 `(ly, 3)` 行已删除，`(ly, 1)`、`(ly, 2)` 保留
- **AND** `out/ly/ep-3/` 目录已不存在
- **AND** `out/ly/keys/ep-3.key`、`ep-3.iv`、`ep-3.key.b64` 已不存在
- **AND** `out/ly/` 目录仍存在（因为还有 ep-1、ep-2）

#### Scenario: 删除一部剧的最后一集清空剧目录
- **GIVEN** 一部剧 `drama_slug=solo` 仅有 `ep_number=1` 一集；`out/solo/ep-1/` 和 `out/solo/keys/ep-1.*` 存在
- **WHEN** 客户端请求 `DELETE /admin/episodes/solo/1`
- **THEN** 响应 200
- **AND** `out/solo/` 整个目录已不存在

### Requirement: encoding 状态守卫

当匹配行存在但 `status == 'encoding'` 时，服务 SHALL 拒绝删除并返回 409 Conflict，响应体包含可读的提示信息（如 "can't delete while encoding"）。

#### Scenario: 正在编码时拒绝删除
- **GIVEN** DB 中 `(ly, 3, status='encoding')`
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应 409
- **AND** DB 行保留不变
- **AND** 磁盘上的文件没有被改动

### Requirement: 不存在的行返回 404

当数据库中找不到 `(drama_slug, ep_number)` 匹配的行时，服务 SHALL 返回 404，响应体包含可读的提示（如 "episode not found"），且不尝试任何磁盘删除。

#### Scenario: 不存在的剧集返回 404
- **WHEN** 客户端请求 `DELETE /admin/episodes/never-seen/1`
- **THEN** 响应 404
- **AND** 磁盘没有任何文件被访问或修改

### Requirement: 管理页删除 UI

管理页 `/admin` 的每条剧集行 SHALL 展示一个"删除"按钮。点击时 SHALL 弹出浏览器 `confirm()` 对话框进行二次确认；确认后前端 SHALL 向 `DELETE /admin/episodes/{slug}/{ep}` 发起请求；成功（HTTP 200）后 SHALL 调用列表刷新（`fetch /admin/episodes`）以从 UI 中移除该行。

#### Scenario: UI 二次确认 + 成功删除后列表更新
- **GIVEN** 用户打开 `/admin`，列表中渲染了 `(ly, 3, ready)`
- **WHEN** 用户点击该行的"删除"按钮
- **THEN** 浏览器弹出 `confirm()` 提示
- **WHEN** 用户点击确认
- **THEN** 前端发起 `DELETE /admin/episodes/ly/3`
- **AND** 收到 200 后调用列表刷新
- **AND** 该行从 UI 中消失（因为后端已删）

#### Scenario: 用户取消二次确认不发起请求
- **GIVEN** 用户打开 `/admin`
- **WHEN** 用户点击删除按钮但在 `confirm()` 对话框点"取消"
- **THEN** 前端 MUST NOT 发起 DELETE 请求
- **AND** 列表保持不变
