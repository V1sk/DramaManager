## Why

管理员上传错集 / 换源 / 下架内容时没有快速纠错的手段 —— 目前只能 SSH 上去手动删 DB 行 + `rm -rf out/...`，容易漏掉 key 文件、空目录残留，也不能在 UI 里立刻看到结果。

## What Changes

- 新增 HTTP 端点 `DELETE /admin/episodes/{drama_slug}/{ep}`：
  - 删除 `episodes` 表中匹配 `(drama_slug, ep_number)` 的行
  - 删除磁盘上 `out/{drama_slug}/ep-{n}/` 整个目录（m3u8 / m4s / init.mp4 / cover.jpg 一起清）
  - 删除磁盘上 `out/{drama_slug}/keys/ep-{n}.key` / `.iv` / `.key.b64` 三个 key 相关文件
  - 若这部剧删到不剩任何集（DB 行数为 0），顺手清空 `out/{drama_slug}/`（包括残留的 `keys/` 空目录）
- 拒绝删除 `status=encoding` 的行，返回 **409 Conflict** —— worker 正在写盘时删会竞争、产生脏片段。`pending` / `ready` / `failed` 均允许删除。
- 磁盘删除失败不阻塞 DB 删除：DB 是真相源，`out/` 下若有零散残留，记 server log warning + 返回体 `warnings` 数组，运维可事后清理。
- 管理页 `/admin` 每行加一个"删除"按钮；点击弹 `confirm("确定删除？")`；确认后 `fetch(..., method: 'DELETE')`；成功后刷新列表。
- 不改 DB schema、不改 `pipeline.sh`、不改 SDK 端点（`/api/episodes/...`、`/api/dramas/...`）。

## Capabilities

### New Capabilities
- `episode-deletion`: 单集删除的端到端行为 —— 端点校验、状态守卫（encoding 拒绝）、DB 行删除、磁盘目录与 key 文件删除、空剧目录清理、失败降级语义、管理页 UI。

### Modified Capabilities
<!-- 不修改已归档的 `hls-management-server` 或活动中的 `sdk-drama-listing` 的 requirement —— 删除是一个独立的管理动作，不影响上传流程和 SDK 读取端点的行为。 -->

## Impact

- **新增代码**：
  - `app/db.py`：新增 `delete_by_slug_ep(drama_slug, ep_number) -> bool` 和 `count_by_slug(drama_slug) -> int`
  - `app/routers/admin.py`：新增 `DELETE /admin/episodes/{slug}/{ep}` handler（含 encoding 409 守卫、磁盘清理、剧目录空则清理）
  - `app/templates/admin.html`：每行一个"删除"按钮 + 二次确认 + JS fetch
- **不动**：DB schema、上传流程、队列、pipeline、SDK 端点、`episode-info-schema.json`
- **对已部署实例**：纯增量。不删除任何现有行为；只是新增一个操作。
