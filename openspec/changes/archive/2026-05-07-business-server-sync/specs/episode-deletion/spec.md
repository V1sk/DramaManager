## MODIFIED Requirements

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

#### Scenario: encoding 状态守卫
- **GIVEN** `(ly, 3)` 的 `status='encoding'`
- **WHEN** 客户端请求 `DELETE /admin/episodes/ly/3`
- **THEN** 响应 409
- **AND** 行 / 磁盘 / OSS 都未变化

#### Scenario: 不存在的行返回 404
- **WHEN** 客户端请求 `DELETE /admin/episodes/never-seen/1`
- **THEN** 响应 404
- **AND** 没有任何 IO 发生
