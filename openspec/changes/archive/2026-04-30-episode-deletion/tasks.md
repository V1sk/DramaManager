## 1. DB 层

- [x] 1.1 在 `app/db.py` 新增 `delete_by_slug_ep(drama_slug: str, ep_number: int) -> bool`：执行 `DELETE FROM episodes WHERE drama_slug=? AND ep_number=?`；返回受影响行数 > 0（供调用方判断命中）。
- [x] 1.2 在 `app/db.py` 新增 `count_by_slug(drama_slug: str) -> int`：`SELECT COUNT(*) FROM episodes WHERE drama_slug=?`。

## 2. HTTP 端点

- [x] 2.1 在 `app/routers/admin.py` 新增 `@router.delete("/admin/episodes/{drama_slug}/{ep}")`：
  - `drama_slug` 用 `Path(..., pattern=r"^[a-z0-9][a-z0-9-]*$")`；`ep` 用 `Path(..., pattern=r"^[0-9]+$")`
  - 调用 `db.get_by_slug_ep` → 404 / 409（encoding）守卫
  - 调用 `db.delete_by_slug_ep` 删 DB 行
  - `shutil.rmtree(settings.out_dir / slug / f"ep-{n}", ignore_errors=False)`，捕获 `OSError` 记 warning
  - `for ext in ("key", "iv", "key.b64"): (settings.out_dir / slug / "keys" / f"ep-{n}.{ext}").unlink(missing_ok=True)` 捕获 OSError 记 warning
  - 若 `db.count_by_slug(slug) == 0`：`shutil.rmtree(settings.out_dir / slug, ignore_errors=True)`
  - 返回 `JSONResponse({"ok": True, "warnings": [...]})`

## 3. 管理页 UI

- [x] 3.1 在 `app/templates/admin.html` 的每条剧集行末尾新增"删除"按钮；按钮上 `data-slug` / `data-ep` 属性。
- [x] 3.2 JS 事件处理：点击按钮 → `confirm("确定删除 {剧名} 第 {ep} 集？...")` → 用户确认 → `fetch(..., method:'DELETE')` → 响应非 200 时 `alert(text)`；成功后调用 `refresh()` 重绘列表。
- [x] 3.3 样式：按钮颜色区分（红色系），和"查看 m3u8 链接"放同一"操作"列。

## 4. 验证

- [x] 4.1 TestClient 正常路径：seed 一行 ready + 假造 `out/{slug}/ep-{n}/` 若干文件 + `out/{slug}/keys/ep-{n}.*` 文件 → DELETE → 200 + warnings=[] + DB 行消失 + 磁盘清理。
- [x] 4.2 TestClient encoding 守卫：seed 一行 status=encoding → DELETE → 409，DB 行保留，磁盘不动。
- [x] 4.3 TestClient 不存在：DELETE 未见过的 (slug, ep) → 404。
- [x] 4.4 TestClient 最后一集清空剧目录：seed 一剧一集 ready + 假造文件 → DELETE → 200 + `out/{slug}/` 整个消失。
- [x] 4.5 TestClient 多集场景：seed 两集 ready，删一集，验证另一集 DB 行和磁盘目录仍在，`out/{slug}/` 未被清空。
- [x] 4.6 TestClient 非法参数：大写 slug、`ep=0`、`ep` 非数字 → 422。
- [x] 4.7 TestClient 部分磁盘失败降级：mock `shutil.rmtree` 抛 OSError → 返回 200 + warnings 非空 + DB 行已删。

## 5. 手工 smoke

- [ ] 5.1 本地启动服务，上传一部剧两集，等 ready；打开 `/admin` 看到两行；点击第一行的"删除"按钮 → 确认 → 页面自动刷新，行消失；`ls out/{slug}/` 确认 `ep-1/` 和 `keys/ep-1.*` 已删，`ep-2/` 和 `keys/ep-2.*` 保留。_(端点逻辑已由 TestClient 场景 4.1/4.5 等效覆盖；本项剩下真实 pipeline + 浏览器 UI 的端到端回归。)_
- [ ] 5.2 删除第二集 → `out/{slug}/` 整个消失。_(由 TestClient 场景 4.4 等效覆盖；真机 UI 回归。)_
