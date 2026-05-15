## Why

SDK 在渲染层需要知道每集源视频的**原始宽高**才能正确处理：

- 计算 aspect ratio，决定 `SurfaceView` / `TextureView` 的尺寸 + 黑边补偿（letterbox / pillarbox）
- 在不同设备屏幕（横屏 / 竖屏 / 异形屏）上预先决定布局，避免首帧后回弹抖动
- 短剧多为竖拍 9:16，但偶有 16:9 的横屏剧；客户端在拉到 m3u8 之前就要预知

目前 `EpisodeInfo` 不返回这两条信息：

- 服务端 ffprobe 在上传时只取了 `duration_ms`，宽高没探测、没存
- DB schema 没有 `width` / `height` 列
- 客户端只能等首帧解码出来后再读 `Format.width/height`，渲染容器要二次调整 → 视觉抖动

## What Changes

**新增 ffprobe 探测**：上传时一次额外 ffprobe 调用拿源视频第一条 video stream 的 codec width / height（不是 SAR 拉伸后的"display dimension"，是原始像素分辨率，与 SDK `MediaItem.format.width/height` 语义一致）。

**`app/ffmpeg_utils.py` 新增** `probe_video_dimensions(src: Path) -> tuple[int, int]`：返回 `(width, height)`；ffprobe 命令为 `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=,:p=0 <src>`，输出形如 `1080,1920`，解析两边都为正整数 raise `FfmpegError`。

**DB schema 新增两列**（`app/db.py::_SCHEMA`）：

```sql
width            INTEGER,
height           INTEGER,
```

两列可空。`init_db` 启动时对老表执行 `ALTER TABLE episodes ADD COLUMN width INTEGER`（同 `height`）做 in-place migration —— 老行的两列保持 NULL，不破坏既有数据。

**上传 handler 写入**（`app/routers/admin.py::admin_upload`）：探测 width/height 后透传给 `db.upsert_pending(..., width=..., height=...)`。失败处理与 `probe_duration_ms` 对齐 —— 探测失败 400 + 清理临时文件。

**`app/db.py::upsert_pending` 接受 width / height**：作为可选 keyword-only 参数（默认 None，兼容潜在旧调用方），写入 INSERT / UPDATE 两路。`bump_updated_at` / `set_status` / `delete_*` / 列表查询不变。

**`app/models.py::EpisodeInfo` 新增字段**：

```python
width: Optional[int] = Field(default=None, ge=1)
height: Optional[int] = Field(default=None, ge=1)
```

**`app/routers/api.py::_row_to_episode_info` 透传**：从 row 取 `width` / `height`，None 即透传 None。单集端点和按剧集数列表端点同时受益（共用 helper）。

**`episode-info-schema.json` 新增字段**：

```jsonc
"width":  { "type": ["integer", "null"], "minimum": 1, "description": "Source video width in pixels (codec width, not display)." },
"height": { "type": ["integer", "null"], "minimum": 1, "description": "Source video height in pixels (codec height)." }
```

不进 `required` —— 老 ready 行 width/height 为 NULL，schema 校验仍要通过。

**老数据兼容策略**：

- DB ALTER 让老行的两列 NULL，schema 接受 null 即可。
- **不**做老行回探：写一次性脚本要扫 `OUT_DIR/{slug}/ep-{n}/720p/init-720p.mp4` 之类，但 init.mp4 是 fMP4 box header，没有原始 codec width/height（已被 scale=-2:720 拉伸过）。要回探必须保留过源视频，而 `pipeline.sh` 跑完后源 mp4 早已 `_cleanup_tmp` 删除 —— 老剧集的源宽高已不可考。
- 客户端策略：拉到的 `width` / `height` 为 null 时退化到首帧解码后再读 —— 即今日行为。新上传的 ready 行直接拿到值，免回弹抖动。
- 不在本 change 提供"重传迁移"建议（行为是渐进式改善，不强制操作员立即重传）。

**不动**：

- `pipeline.sh` 与三个 stage 脚本（不需要看宽高）
- m3u8 / 切片产物
- 编码 ladder（仍是 540p / 720p / 1080p 固定档位）
- `DramaSummary` 形态
- 管理页 `/admin` UI（不渲染宽高字段；运维只关心 status）
- 已归档的 OSS 双 host 拓扑（width/height 是 SDK 字段，与 host 归属无关）

## Capabilities

### New Capabilities

- `episode-video-dimensions`: 上传时 ffprobe 探测源视频 codec width / height；存 DB 两列；在 `EpisodeInfo` 透传给 SDK；老行兼容 null。

### Modified Capabilities

<!-- 不修改任何已存在的 requirement —— EpisodeInfo 的 width/height 是新增可选字段，
     既不打破单集 / 列表端点的现有契约，也不动 host-relative URL / OSS 双形态语义。 -->

## Impact

- **新增代码**：
  - `app/ffmpeg_utils.py`：新增 `probe_video_dimensions`。
  - `app/db.py::_SCHEMA`：两列；`init_db` 加 ALTER 兼容；`upsert_pending` 加两个可选参数。
  - `app/routers/admin.py::admin_upload`：调 `probe_video_dimensions` + 透传。
  - `app/models.py::EpisodeInfo`：两个 `Optional[int]` 字段。
  - `app/routers/api.py::_row_to_episode_info`：从 row 透传。
  - `episode-info-schema.json`：两个 `["integer","null"]` 字段；examples 之一加上具体值（保持另一个 null 示例）。
- **不动**：pipeline / stage 脚本 / 编码 ladder / 管理页 / DramaSummary / OSS 拓扑。
- **DB 迁移**：lifespan 启动时自动 ALTER；老数据零损失，新列 NULL。
- **文档**：`CLAUDE.md` 更新 "Cross-system contracts" 与 EpisodeInfo phases 段落（width/height 是 Phase 1 透传，可空）。
- **对已部署实例**：纯增量。重启即生效；新上传写新值，老 ready 行返回 `null`。
