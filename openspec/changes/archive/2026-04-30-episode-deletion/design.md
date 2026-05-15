## Context

删除是破坏性操作，需要明确几个小决策以免留下脏数据或卡住 worker。上传流程、队列、pipeline 和 SDK 端点全都不动，这个 change 只加一条操作路径。

## Goals / Non-Goals

**Goals:**
- 单集粒度的端到端删除（DB + 磁盘），由管理页一键触发
- 不把 worker 正在跑的集删掉
- 空剧目录自动清理，不积累零散残留

**Non-Goals:**
- 整部剧一键删除（客户端如需可循环调用单集删除）
- 软删除 / 回收站（内部系统，简单就好）
- 删除审计日志（server log WARNING 够用；有需要再做）
- 删除时通知 SDK 客户端（SDK 下次查询自然发现该集已消失）

## Decisions

### D1. `status=encoding` 拒绝删除（409）；其他状态都允许

**理由**：worker 正在写 `out/{slug}/ep-{n}/` 下的 m3u8 / m4s 时删目录会竞争 —— 产生两种坏结果：(a) worker 完成时 `set_status(ready)` 成功，但磁盘只剩 worker 写的一半、`media-720p.m3u8` 缺失，SDK 查询就能拿到但播放会 404；(b) 或 worker 写到一半报错 → `set_status(failed)` 在已删除的行上，UPDATE 无效（无记录），但 DB 行也不在了，倒没什么问题。整体权衡下来拒绝 encoding 更稳。

`pending`：队列里还没起，允许删 —— worker 取到 job 后 pipeline 会因文件缺失失败，`set_status(failed)` 更新 0 行（因为 DB 行已删），不会报错，只是 log 有错误。可接受。

`ready` / `failed`：典型删除目标，直接删。

### D2. 磁盘删除失败降级为 DB 删除成功 + warning，不阻断

**理由**：
- DB 是真相源 —— 行删了 SDK / 管理页立即生效，用户感知到"删了"
- 磁盘残留是运维问题，不影响正确性（空目录 / 零散 key 文件不会被任何代码路径访问到）
- 强一致反而更糟：如果 DB 删成功了磁盘再失败，rollback DB 要么失败要么产生跨事务的不一致窗口。简单的"DB 优先 + warning 降级"语义清晰、实现简单

**响应体设计**：`{"ok": true, "warnings": [path, ...]}`。成功路径 `warnings=[]`；有残留时列出来，管理员 UI 看一眼就知道要不要去手动清。

### D3. 删完最后一集顺手清空整个剧目录

删完 `out/{slug}/ep-{n}/` 和 `out/{slug}/keys/ep-{n}.*` 后，若 DB 中 `count_by_slug(slug) == 0`（该剧再无任何行），直接 `shutil.rmtree(out/{slug}, ignore_errors=True)`。这样不会留下空的 `{slug}/` 和 `{slug}/keys/` 空壳。

**理由**：剧目录只在有集时才有意义；剧删光了留个空目录又没信息量，也会让剧目录列表出现幻影数据（不过 `list_ready_dramas` 已经用 `WHERE status='ready' GROUP BY drama_slug` 过滤，磁盘空目录本身不会显示，但对运维直接 `ls out/` 时造成噪音）。

**注意**：如果用户同时在同一剧 re-upload 下一集，`out/{slug}/` 会被再次创建 —— upload handler 里的 `mkdir(parents=True, exist_ok=True)` 本就容忍，没问题。

### D4. 端点放 `/admin/` 而不是 `/api/`

DELETE 是管理动作，放在 `/admin/` 下与"删除是内部操作"的心智一致；`/api/` 保留给 SDK 消费。这也和当前 `GET /admin/episodes`（列表）同前缀，方便运维用同一套逻辑追踪。

## Risks / Trade-offs

- **[Risk] 用户点错删除按钮**：JS `confirm()` 是唯一护栏，没有 undo。**Mitigation**：内部系统、剧集可以重新上传，不算不可逆的数据损失。
- **[Risk] 删除 `pending` 状态的集：worker 从队列取到时 pipeline.sh 会因 source tmp 被 worker 自己清理 / out dir 被删而失败**。**Mitigation**：D1 已接受这个代价（worker 容错路径本就健壮）。
- **[Trade-off] 没有整部剧删除**：用户需要循环调单集。内部系统量级够用；未来要加只需要一个 `DELETE /admin/dramas/{slug}` 包上 `shutil.rmtree(out/{slug})` + DB `DELETE WHERE drama_slug=?`。

## Open Questions

无。
