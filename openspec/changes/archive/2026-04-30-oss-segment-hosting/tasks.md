## 1. 配置层

- [x] 1.1 `app/config.py::Settings` 新增字段 `oss_enabled: bool`（默认 `False`）。
- [x] 1.2 在 `Settings.from_env()`（或等价构造点）读取 env `OSS_ENABLED`：值在 `{"true", "1", "yes"}`（大小写不敏感）时为 `True`，其它为 `False`。
- [x] 1.3 `app/main.py` lifespan 启动日志新增 `oss_enabled=...` 一行；`oss_enabled=True` 时附带 `oss_public_base_url=...`（不打凭证）。

## 2. `app/oss_upload.py` 暴露公网常量

- [x] 2.1 在 `app/oss_upload.py` 顶部新增 module-level 常量 `oss_public_base_url = "https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama"`（值由 bucket 名 + endpoint host + `ossBaseDir` 派生；本 change 不动 endpoint / bucket / 凭证 / `ossBaseDir`）。
- [x] 2.2 不修改 `upload_file` 函数签名 / `ossBaseDir` 值 / `bucket` 实例化方式。

## 3. m3u8 改写

- [x] 3.1 新增 `app/publish.py`：实现 `rewrite_playlist(text: str, oss_base: str) -> str`，按 spec 的"m3u8 改写规则"表逐行处理。函数内部对 `oss_base` 调 `.rstrip('/')`，避免输入尾斜杠造成 `//`。
- [x] 3.2 单测覆盖 spec 里的五条 scenario：`#EXT-X-MAP` 行 URI 替换、`#EXT-X-KEY` 行透传、segment 行前缀、元数据行透传、再次改写幂等。
- [x] 3.3 单测:把一段真实 ffmpeg 产出 m3u8（在 `tests/fixtures/sample-720p.m3u8` 放一份）跑一次 rewrite，断言 `#EXT-X-KEY` 行字节不变、所有 `seg-720p-N.m4s` 都被替换、`init-720p.mp4` 在 `#EXT-X-MAP:URI=` 里被替换。

## 4. publish 编排

- [x] 4.1 `app/publish.py` 新增 `publish_ladder(slug: str, ep_dir: str, ladder: str) -> None`：
  - `from .config import settings`
  - `from .oss_upload import upload_file, ossBaseDir, oss_public_base_url`
  - 遍历 `settings.out_dir / slug / ep_dir / ladder` 下 `init-{ladder}.mp4` + `seg-{ladder}-*.m4s`，对每个文件：
    ```python
    oss_path = f"{ossBaseDir}/{slug}/{ep_dir}/{ladder}/{filename}"   # 不以 / 开头
    res = upload_file(oss_path, str(local_path))
    if not res.get("result"):
        raise PublishError(f"OSS upload failed for {ladder} {filename}: {res}")
    ```
  - 读取 `media-{ladder}.m3u8`，调 `rewrite_playlist(text, f"{oss_public_base_url}/{slug}/{ep_dir}/{ladder}")` 改写后写回原路径（用 `Path.write_text`，保留换行）。
  - 自定义 `class PublishError(Exception)`。
- [x] 4.2 `app/queue.py::_handle_job`：pipeline 成功后，若 `settings.oss_enabled`：用 `await asyncio.to_thread(publish_ladder, slug, ep_dir, "540p")` 等三档串行调用。任一档抛 `PublishError` 或其它异常 → `db.set_status(ep_id, 'failed', error_message=str(e))` 并 `return`，不动后续档；本地产物保留。
- [x] 4.3 OSS 未启用时 `_handle_job` 行为**不变**（不调 `publish_ladder`）—— 用 `if settings.oss_enabled` 守门即可。

## 5. EpisodeInfo URL 形态切换

- [x] 5.1 `app/routers/api.py::_row_to_episode_info`：从 `..config import settings`、`..oss_upload import oss_public_base_url` 引入；根据 `settings.oss_enabled` 选择 base：
  ```python
  if settings.oss_enabled:
      media_base = f"{oss_public_base_url}/{slug}/{ep_dir}"   # 用于 init/firstSeg
  else:
      media_base = f"/videos/{slug}/{ep_dir}"
  ```
  `initUrl` / `firstSegUrl` 用 `media_base` 拼；`playUrl` / `fallback.low/.high` / `coverUrl` / `drm.keyUri` 不变（永远走 `/videos/...` 或 `/drm/...` 相对路径）。
- [x] 5.2 helper 加 1-2 行注释说明双形态语义。
- [x] 5.3 TestClient 验证四组 Scenario：
  - `OSS_ENABLED=true` + 单集端点 → `initUrl` / `firstSegUrl` 以 `oss_public_base_url` 开头、其它字段相对路径
  - `OSS_ENABLED=true` + 列表端点 → 同上，且对同一行单集 / 列表元素逐字节一致
  - `OSS_ENABLED` 未设 + 单集端点 → 所有字段相对路径（行为同今日）
  - `OSS_ENABLED` 未设 + 列表端点 → 同上
- [x] 5.4 TestClient：两种模式下都跑 `episode-info-schema.json` 严格校验（`Draft202012Validator(format_checker=...)`），都通过。

## 6. 迁移脚本

- [x] 6.1 新增 `scripts/migrate_to_oss.py`：CLI 脚本（不在服务进程内运行）。读取 `Settings`，断言 `settings.oss_enabled` 为真；遍历 `db.list_ready_dramas()` → 每剧 → `db.list_ready_by_slug(slug)` → 每行 → 三档调 `publish_ladder`。失败行打印 + 计数继续，最后输出 `成功 X 行 / 失败 Y 行` 摘要。
- [x] 6.2 脚本幂等：依赖 `rewrite_playlist` 的"再次改写是 noop"约束（spec scenario "已改写的 m3u8 再次改写是 no-op"）；上传层重复传同一对象 oss2 默认覆盖。
- [x] 6.3 README / CLAUDE.md 加一段 "Migration"，说明启用 OSS 后跑此脚本一次。

## 7. 文档

- [x] 7.1 `CLAUDE.md` env 表新增 1 行 `OSS_ENABLED`；说明默认 false、设 `true` 时 worker 会把切片传到 OSS。
- [x] 7.2 `CLAUDE.md` URL map 注释 `initUrl` / `firstSegUrl` 在 `OSS_ENABLED=true` 时是 OSS 绝对 URL；其它字段始终业务 host 相对路径。
- [x] 7.3 `CLAUDE.md` 新增段落 "## OSS 双 host 拓扑"：解释为什么 m3u8 留业务 host、key 留业务 host、切片去 OSS；附 m3u8 示例（同时含相对的 `#EXT-X-KEY:URI` 和绝对的 `#EXT-X-MAP:URI` / segment 行）；说明 OSS 桶必须配置 CORS（来源 = 业务 host）才能在 hls.js / 浏览器播放；说明凭证当前硬编码在 `app/oss_upload.py`，多环境部署 / 凭证轮换需另开 follow-up。
- [x] 7.4 `CLAUDE.md` "Cross-system contracts" 段落更新："`#EXT-X-KEY:URI` 与 `EpisodeInfo.drm.keyUri` 永远 verbatim 一致" 这条契约措辞保持，但补一句"`#EXT-X-MAP:URI` 与 segment 行可以是绝对 OSS URL，不与任何 API 字段比对"。

## 8. 手工 smoke

- [ ] 8.1 本地 `OSS_ENABLED` 未设：上传一集，确认 `EpisodeInfo` 全相对、播放正常（行为同今日）。
- [ ] 8.2 设 `OSS_ENABLED=true` 重启，上传一集；阿里云 OSS 控制台或 `oss2` 列对象验证 `Drama/{slug}/ep-{n}/{rung}/init-{rung}.mp4` + `seg-{rung}-*.m4s` 都在桶里；`curl /api/episodes/{slug}/{ep}` 验证 `initUrl` / `firstSegUrl` 是绝对 OSS URL；hls.js demo 页播放正常（确认 OSS 桶 CORS 已配业务 host Origin）。
- [ ] 8.3 临时把 `app/oss_upload.py` 里 access key 改错，重启 + 上传一集 → status 应转为 failed，error_message 含 OSS 错误信息；本地 `out/{slug}/ep-{n}/` 仍在；改回正确凭证。
- [ ] 8.4 已 ready 的老剧（`OSS_ENABLED` 未设时上传的）：开启 `OSS_ENABLED=true` + 跑 `python scripts/migrate_to_oss.py`，确认 OSS 桶里有切片、`/api/episodes/...` 的 `initUrl` 切到了 OSS；播放正常。
- [ ] 8.5 `OSS_ENABLED=true` 下手工删除一集（`DELETE /admin/episodes/...`）：本地目录清空、DB 行删；OSS 端会留下孤儿对象（**预期行为**，本 change 范围内不联动 OSS 删除）—— 在 8.5 完成后人工去 OSS 控制台清一下，并记录到 follow-up（见 9.1）。

## 9. 范围外（Open Questions / Follow-ups 跟踪）

- [ ] 9.1（follow-up change）OSS 端删除联动：在 `app/oss_upload.py` 添加 `delete_prefix(oss_prefix)` helper；改 `DELETE /admin/episodes/{slug}/{ep}` handler 在本地删除后调用；失败 `warnings: ["oss cleanup partial: ..."]` 降级。
- [ ] 9.2（follow-up）OSS 凭证从硬编码改成 env / vault：`app/oss_upload.py` 顶部 `accessKeyId` / `accessKeySecret` 走 `os.environ`；多环境 / 多桶时再扩。
- [ ] 9.3（follow-up）`POST /admin/episodes/{slug}/{ep}/republish`：单独重跑 `publish_ladder` 三档（不重转码），用于"OSS 间歇性失败 → 自助重发"。
- [ ] 9.4（follow-up）OSS 桶私有 + 业务侧签发预签名 URL：与"加鉴权"那波 change 一起做。
