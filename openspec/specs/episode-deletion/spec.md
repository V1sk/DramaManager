# episode-deletion

单集删除端点 + 状态守卫 + 磁盘清理 + 管理页 UI。归档自 `2026-04-30-episode-deletion`。

## Requirements

### Requirement: 单集删除端点

服务 SHALL 提供 `DELETE /admin/episodes/{drama_slug}/{ep}` 端点。`drama_slug` 必须匹配 `^[a-z0-9][a-z0-9-]*$`，`ep` 必须匹配 `^[0-9]+$`，否则 422。当匹配的行不存在时 → 404。当匹配的行存在但 `status == 'encoding'` 时 → 409。

匹配的行存在且 `status != 'encoding'` 时，handler SHALL 根据 `last_synced_at` 分支执行：

**分支 A — 从未同步过** (`last_synced_at IS NULL`):
1. 从 `episodes` 表删除该行。
2. 删除 `out/{drama_slug}/ep-{ep}/` 整个目录（`shutil.rmtree`，容忍目录不存在）。
3. 删除 `out/{drama_slug}/keys/ep-{ep}.key`、`.iv`、`.key.b64` 三个文件（容忍不存在）。
4. 当 `settings.oss_enabled` 时，调用 `publish.unpublish_episode_from_staging(drama_slug, "ep-{ep}")`。
5. 响应 `200 {"ok": true, "warnings": [...]}`。

**分支 B — 之前同步过** (`last_synced_at IS NOT NULL`):
1. 更新该行：`sync_status='pending_delete'`，刷新 `updated_at`。**行保留。**
2. 删除 `out/{drama_slug}/ep-{ep}/` 整个目录（操作员已经不要本地文件了）。
3. 删除三个 keys 文件。
4. 当 `settings.oss_enabled` 时，调用 `publish.unpublish_episode_from_staging(drama_slug, "ep-{ep}")`。
5. 响应 `200 {"ok": true, "warnings": [...], "pending_sync": true}`。
6. 行的物理删除发生在后续 `DELETE /sync/episodes/{slug}/{ep}` 同步成功之后（详见 `business-server-sync` capability 的 sync worker 行为）。

服务 SHALL NOT 删除 `out/{drama_slug}/` 整个目录（剧目录的清理归 `drama-entity` capability 的 `DELETE /admin/dramas/{slug}` 端点负责）。

任何磁盘 / OSS 删除失败 MUST NOT 阻塞 DB 操作；失败路径 SHALL 记入 server log 并出现在响应体的 `warnings` 数组中。

#### Scenario: 从未同步的集 → 物理删除
- **GIVEN** `dramas` 中存在 `slug='ly'`；`episodes` 中 `(ly, 3)` 的 `last_synced_at IS NULL`，`status=ready`；本地产物 + keys 文件存在
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应是 `200 {"ok": true, ...}`，不包含 `pending_sync`
- **AND** `episodes` 中 `(ly, 3)` 行已删除
- **AND** `out/ly/ep-3/` 目录已不存在
- **AND** keys 三件套不存在
- **AND** `out/ly/` 目录仍存在（如果同剧还有其它集）

#### Scenario: 同步过的集 → pending_delete 不删除行
- **GIVEN** `(ly, 3)` 的 `last_synced_at` 不为 NULL，`status=ready`，`sync_status='clean'`
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应是 `200 {"ok": true, ..., "pending_sync": true}`
- **AND** `episodes` 中 `(ly, 3)` 行仍存在但 `sync_status='pending_delete'`
- **AND** 本地产物 / keys / staging OSS 对象 都已清理
- **AND** prod OSS 对象 unchanged（清理由后续 sync 触发）

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

每条剧集的删除按钮 SHALL 出现在两处：
1. 剧详情页 (`/admin/dramas/{slug}`) 的集列表表格中每一行（除非该集状态为 `encoding`）。
2. 单集详情页 (`/admin/dramas/{slug}/episodes/{ep}`) 的页面操作区。

点击删除按钮 SHALL 弹出浏览器 `confirm()` 对话框进行二次确认；确认后前端 SHALL 向 `DELETE /admin/episodes/{slug}/{ep}` 发起请求。

成功（HTTP 200）后的导航行为：
- **从剧详情页触发**：调用页面刷新（reload 或重新拉取 `/admin/dramas/{slug}/full` 并重渲染集列表），从 UI 中移除该行。
- **从单集详情页触发**：浏览器导航回 `/admin/dramas/{slug}`。

旧版的单页扁平列表 (`/admin` 上的剧集表格中的删除按钮) 已被 `admin-redesign` 替换为剧目卡片首页，不再承载剧集级删除操作；该旧 UI 路径已不存在。

#### Scenario: 从剧详情页删除一集
- **GIVEN** 用户打开 `/admin/dramas/ly`，该剧有集 1、2、3，状态分别为 ready、encoding、ready
- **THEN** 集 1 和集 3 的行有"删除"按钮；集 2 的行没有（因为 status=encoding）
- **WHEN** 用户点击集 3 行的删除按钮并在 `confirm()` 中确认
- **THEN** 前端发起 `DELETE /admin/episodes/ly/3`
- **AND** 收到 200 后页面刷新（或局部重渲染）
- **AND** 集 3 的行从表格中消失

#### Scenario: 从单集详情页删除并跳转回剧详情
- **GIVEN** 用户打开 `/admin/dramas/ly/episodes/3`
- **WHEN** 用户点击"[删除本集]"按钮并确认
- **THEN** 前端发起 `DELETE /admin/episodes/ly/3`
- **AND** 收到 200 后浏览器导航到 `/admin/dramas/ly`

#### Scenario: 用户取消二次确认不发起请求
- **WHEN** 用户点击删除按钮但在 `confirm()` 对话框点"取消"
- **THEN** 前端 MUST NOT 发起 DELETE 请求
- **AND** 页面保持不变
