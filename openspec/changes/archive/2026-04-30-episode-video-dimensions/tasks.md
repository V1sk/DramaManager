## 1. ffprobe 探测函数

- [x] 1.1 在 `app/ffmpeg_utils.py` 新增 `probe_video_dimensions(src: Path) -> tuple[int, int]`：执行 `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=,:p=0 <src>`，期待输出形如 `1080,1920`。解析两个正整数；解析失败 / 非正整数 / ffprobe 非零退出 → raise `FfmpegError`，错误信息含 ffprobe stderr 尾部。
- [x] 1.2 单测：在 `tests/test_ffmpeg_utils.py` 用一个真实 `tests/fixtures/sample-720p.mp4`（如无可生成：`ffmpeg -f lavfi -i testsrc=duration=1:size=720x1280:rate=30 -y tests/fixtures/sample-720x1280.mp4`）跑 `probe_video_dimensions`，断言返回 `(720, 1280)`。

## 2. DB schema + helper

- [x] 2.1 `app/db.py::_SCHEMA` 在 `cover_url` 行之后追加 `width INTEGER,` 与 `height INTEGER,` 两列（声明可空）。
- [x] 2.2 `app/db.py::init_db()` 在 `executescript(_SCHEMA)` 之后追加幂等 ALTER：对 `("width", "height")` 各试一次 `ALTER TABLE episodes ADD COLUMN <col> INTEGER`，捕获 `sqlite3.OperationalError` 静默通过（已存在）。
- [x] 2.3 `app/db.py::upsert_pending` 签名加两个 keyword-only 可选参数 `width: int | None = None`、`height: int | None = None`，INSERT / UPDATE 两路都写入。`set_status` / `bump_updated_at` / `delete_*` / `list_*` / `get_by_slug_ep` 不动（`SELECT *` 自动带新列）。
- [x] 2.4 单测：用临时 SQLite 文件先建一张老 schema（不含 width / height）+ 灌一行老数据；重新初始化新 `_SCHEMA` + 调 `init_db`；断言新表含 width / height 两列、老行 width / height 为 NULL、其它字段不变。

## 3. 上传 handler 接入

- [x] 3.1 `app/routers/admin.py::admin_upload`：在 `probe_duration_ms` 调用之后、`extract_first_frame` 之前，调 `probe_video_dimensions(tmp_path)`；失败处理与 duration 探测同款（unlink + 400）。
- [x] 3.2 把宽高传给 `db.upsert_pending(..., width=..., height=...)`。
- [x] 3.3 import 新增：`probe_video_dimensions` 加到 `from ..ffmpeg_utils import ...` 行。

## 4. EpisodeInfo 模型 + helper

- [x] 4.1 `app/models.py::EpisodeInfo` 新增字段：
  ```python
  width: Optional[int] = Field(default=None, ge=1)
  height: Optional[int] = Field(default=None, ge=1)
  ```
  位置放在 `coverUrl` 之后、`initUrl` 之前（保持 schema 字段顺序的视觉一致）。
- [x] 4.2 `app/routers/api.py::_row_to_episode_info`：构造 `EpisodeInfo` 时透传 `width=row.get("width")`、`height=row.get("height")`。

## 5. JSON schema

- [x] 5.1 `episode-info-schema.json` 在 `coverUrl` 之后、`initUrl` 之前追加：
  ```jsonc
  "width":  { "type": ["integer", "null"], "minimum": 1, "description": "Source video codec width in pixels (not display dimension). May be null for episodes uploaded before this field existed." },
  "height": { "type": ["integer", "null"], "minimum": 1, "description": "Source video codec height in pixels (not display dimension). May be null for episodes uploaded before this field existed." },
  ```
- [x] 5.2 examples[0] 加上 `"width": 720, "height": 1280`；examples[1]（drm: null 那一例）保持不带 width/height（演示 "字段缺失" 与 null 都合法）。

## 6. TestClient 验证

- [x] 6.1 `tests/test_episode_video_dimensions.py`：
  - 准备临时 OUT_DIR + DB；通过 db helper 直接灌一行 `status=ready, width=720, height=1280`。
  - `GET /api/episodes/{slug}/{ep}` → 断言响应 `width == 720`、`height == 1280`、用 `Draft202012Validator(format_checker)` 跑 schema 严格校验。
  - `GET /api/dramas/{slug}/episodes` → 断言列表元素与单集逐字节相等（含 width/height）。
- [x] 6.2 同一文件加一个老行 case：灌一行 `status=ready, width=NULL, height=NULL`（直接 SQL 或省略 width/height 走 `upsert_pending` 默认）。断言响应中 `width is None`、`height is None`、schema 严格校验通过。

## 7. 文档

- [x] 7.1 `CLAUDE.md` "Cross-system contracts" 段落 `episode-info-schema.json` 那一行的列表加一句："`width` / `height` 是源视频 codec dimension（与 SDK `Format.width/height` 同口径），新上传写入；老 ready 行可能为 null。"
- [x] 7.2 `CLAUDE.md` Upload lifecycle 第 3 步从 "ffprobe → duration_ms" 更新为 "ffprobe → duration_ms + width + height（codec dimension）"。

## 8. 手工 smoke

- [ ] 8.1 启动服务上传一集竖拍剧（如 720×1280）；`/admin/episodes` 看 status 进入 ready；`curl /api/episodes/{slug}/{ep} | jq '{width, height}'` 拿到 `{"width":720,"height":1280}`。
- [ ] 8.2 上传一集横拍源（如 1920×1080）；同样验证。
- [ ] 8.3 在升级前的库（含老 ready 行）上重启后跑 `curl /api/episodes/{老 slug}/{老 ep} | jq '{width, height}'` → `{"width":null,"height":null}`；其它字段不变；schema 严格校验仍通过（用 `python -c "import json; from jsonschema import ..."`）。
