## 1. 重构 —— 抽出共享的 row → EpisodeInfo helper

- [x] 1.1 在 `app/routers/api.py` 新增模块级函数 `_row_to_episode_info(row: dict) -> EpisodeInfo`：搬入现有 `get_episode` 里的字段映射（`episodeId` / `playUrl` / `durationMs` / `coverUrl` / `drm.{keyUri,keyBase64,ivHex}`），包括 "当 `key_uri` 和 `key_b64` 都存在时才构造 drm、否则 drm=None" 的分支。
- [x] 1.2 让 `get_episode` 在 404 判断后调用 `_row_to_episode_info(row)`，不再内联字段映射。
- [x] 1.3 通过 TestClient 回归 `GET /api/episodes/{slug}/{ep}`：构造一条 `status=ready` 的伪造行，确认响应 body 与重构前完全一致（字段名、取值、drm 结构）。

## 1b. EpisodeInfo 填充 `initUrl` / `firstSegUrl` / `fallback`

- [x] 1b.1 在 `app/models.py` 新增 `FallbackPlaylists` 模型（`low: Optional[str]`、`high: Optional[str]`）；给 `EpisodeInfo` 加上 `initUrl: Optional[str]`、`firstSegUrl: Optional[str]`、`fallback: Optional[FallbackPlaylists]` 三个字段。
- [x] 1b.2 在 `_row_to_episode_info` 里按设计文档 D6 的规则拼四个 URL 并填入返回对象。
- [x] 1b.3 TestClient 验证：构造一行 ready 数据，`GET /api/episodes/ly/3` 响应体包含四个推导 URL，等于设计文档 D6 里的字符串；用 `jsonschema` 对该响应跑 `episode-info-schema.json` 校验；同一行 `GET /api/dramas/ly/episodes` 中对应元素与单集响应逐字段等价。

## 1c. URL host-relative 化 + 废弃 `PUBLIC_BASE_URL` + 修复 URL 段 pre-existing bug

- [x] 1c.1 `app/queue.py::_handle_job`: 构造 `key_uri = f"/drm/{slug}/ep-{ep_number}/key"` 和 `play_url = f"/videos/{slug}/ep-{ep_number}/720p/media-720p.m3u8"`；引入 `ep_dir = f"ep-{job.ep_number}"` 作为 URL 段、pipeline 第 3 参数、key 文件名前缀；DB 查询继续用完整 `episode_id`。
- [x] 1c.2 `app/routers/admin.py`: 上传时 `cover_url = f"/videos/{drama_slug}/{ep_dir_name}/cover.jpg"`（相对路径）。
- [x] 1c.3 `app/routers/api.py::_row_to_episode_info`: 引入 `ep_dir = f"ep-{row['ep_number']}"`，`base = f"/videos/{slug}/{ep_dir}"`，三处推导 URL 全部走相对路径。
- [x] 1c.4 `app/config.py`: 从 `Settings` 中移除 `public_base_url` 字段；删除 `_require_base_url`；`PUBLIC_BASE_URL` 环境变量不再读取。
- [x] 1c.5 `app/main.py`: lifespan 启动日志去掉 `public_base_url`。
- [x] 1c.6 `episode-info-schema.json`: 7 处 `"format": "uri"` 改为 `"format": "uri-reference"`（兼容相对路径的严格校验）。
- [x] 1c.7 TestClient 严格校验（使用 `jsonschema.Draft202012Validator(schema, format_checker=...)`）：
  - 单集、单剧集数列表、剧目录返回的所有 URL 均以 `/` 开头、不含 `//`、不含 `://`
  - `GET /drm/ly/ep-3/key` 返回 200 + 16 字节（修复前是 422）
  - 单集响应和列表中对应元素字段字节相等
  - `drm.keyUri` 形如 `/drm/{slug}/ep-{n}/key`；DB 的 `episodeId` 字段形如 `{slug}-ep-{n}`（两者一并校验）
- [x] 1c.8 `CLAUDE.md`: 启动命令删掉 `PUBLIC_BASE_URL=...`，env 表去掉该行，新增一段说明 URL 全部相对路径；lifecycle 第 7 步的 pipeline 命令改为 `.../drm/{slug}/ep-{n}/key`。
- [x] 1c.9 兼容旧产物：`app/routers/drm.py` 放宽 `episode_id` pattern 到 `^[a-z0-9][a-z0-9-]*$`（与 `drama_slug` 一致），handler 直接用 URL 段做 key 文件名前缀。TestClient 验证两种形式（`ep-3` / `{slug}-ep-7`）都 200；非法 slug 仍 422；不存在文件仍 404。

## 2. 新增 `DramaSummary` 模型

- [x] 2.1 在 `app/models.py` 新增 Pydantic 模型 `DramaSummary`，字段：
  - `dramaSlug: str`
  - `dramaName: str`
  - `epCount: int` （`ge=1`）
  - `latestEpNumber: int` （`ge=1`）
  - `posterUrl: Optional[str] = None`
  - `lastUpdatedAt: str`（ISO 8601 字符串，直接透传 DB 的 `updated_at`）

## 3. 新增单剧集数列表 endpoint

- [x] 3.1 在 `app/db.py` 新增 `list_ready_by_slug(drama_slug: str) -> list[dict]`：执行 `SELECT * FROM episodes WHERE drama_slug=? AND status='ready' ORDER BY ep_number ASC`，返回 list of dict。
- [x] 3.2 在 `app/routers/api.py` 新增 handler `GET /api/dramas/{drama_slug}/episodes`：
  - `drama_slug` 用 `Path(..., pattern=r"^[a-z0-9][a-z0-9-]*$")` 约束（不匹配自动 422）
  - 调用 `db.list_ready_by_slug`，对每行调用 `_row_to_episode_info`
  - 返回 `JSONResponse([info.model_dump(exclude_none=False) for info in infos])`
  - 空 list 时返回 `JSONResponse([])`（不做 404）
- [x] 3.3 TestClient 验证：
  - 空 DB → `GET /api/dramas/anything/episodes` 返回 `200 []`
  - 非法 slug `Bad_Slug` 返回 422
  - DB 写入同 slug 下 ep 1/2/3/4/5，status 依次为 ready/ready/encoding/failed/ready：请求列表，验证返回 3 条、按 ep 升序（1、2、5）、每条含完整 drm
  - 同一行用单集接口和列表接口分别取出做字段比对，取值完全一致（spec "单集和列表中的同一集对象等价" scenario）

## 4. 新增剧目录 endpoint

- [x] 4.1 在 `app/db.py` 新增 `list_ready_dramas() -> list[dict]`，执行设计文档 D4 里的聚合 SQL：
  ```sql
  SELECT e.drama_slug, MAX(e.drama_name) AS drama_name,
         COUNT(*) AS ep_count, MAX(e.ep_number) AS latest_ep_number,
         MAX(e.updated_at) AS last_updated_at,
         (SELECT e2.cover_url FROM episodes e2
            WHERE e2.drama_slug=e.drama_slug AND e2.status='ready'
            ORDER BY e2.ep_number ASC LIMIT 1) AS poster_url
  FROM episodes e WHERE e.status='ready'
  GROUP BY e.drama_slug
  ORDER BY last_updated_at DESC, e.drama_slug ASC
  ```
  返回 list of dict，键名同上。
- [x] 4.2 在 `app/routers/api.py` 新增 handler `GET /api/dramas`：调用 `db.list_ready_dramas()`，把每行映射成 `DramaSummary`（`drama_slug → dramaSlug`、`drama_name → dramaName`、`ep_count → epCount`、`latest_ep_number → latestEpNumber`、`last_updated_at → lastUpdatedAt`、`poster_url → posterUrl`），返回 `JSONResponse([d.model_dump() for d in summaries])`。
- [x] 4.3 TestClient 验证：
  - 空 DB / 无 ready 记录 → `GET /api/dramas` 返回 `200 []`
  - 写入三部剧（a 2 集 ready / b 3 集 ready / c 1 集 ready），设定不同 `updated_at`：验证顺序按 `lastUpdatedAt` 降序、次级按 `dramaSlug` 升序
  - 写入一部剧 `drama_slug=x`，`ep_number=1 failed`、`ep_number=2 ready cover=URL2`、`ep_number=3 ready cover=URL3`：验证该剧 `posterUrl == URL2`（最小 ready ep）
  - 写入一部剧所有行都是 `encoding`/`failed`：验证 `GET /api/dramas` 返回的数组不含它

## 5. 文档

- [x] 5.1 在 `CLAUDE.md` 的 "URL map" 表中，`GET /api/episodes/{slug}/{ep}` 那一行之后新增两行：
  - `GET /api/dramas` — SDK 剧目录，`DramaSummary[]` 按最近更新降序；空目录返回 `[]`
  - `GET /api/dramas/{slug}/episodes` — 某剧所有 `status=ready` 的集，`EpisodeInfo[]` 按 `ep_number` 升序；空结果返回 `[]`

## 6. 手工 smoke

- [ ] 6.1 本地启动服务，上传两部剧、每部 2-3 集。等 pipeline 全部跑到 `ready`。_(需要真实 pipeline 运行，端点逻辑已由 TestClient 基于 seed data 等效覆盖。)_
- [x] 6.2 `curl http://localhost:8000/api/dramas | jq` 验证：两部剧都出现；字段齐全；`posterUrl` 是第 1 集的 cover URL；按 `lastUpdatedAt` 降序。
- [x] 6.3 `curl http://localhost:8000/api/dramas/<slug>/episodes | jq '.[] | {episodeId, ep_num: .durationMs}'` 验证：集数按升序；每条含完整 drm。
- [x] 6.4 `curl -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/dramas/Bad_Slug/episodes` 验证返回 422。
- [x] 6.5 `curl http://localhost:8000/api/dramas/never-seen/episodes` 验证返回 `200 []`。
- [ ] 6.6 把某一集手动改成 `status=failed`（直接 UPDATE DB），验证剧目录中该剧的 `epCount` 和 `latestEpNumber` 相应减少；`posterUrl` 在受影响时按规则切换。_(poster 切换规则已在 TestClient 场景 D 用等效 seed data 验证；此任务剩下的价值是真实环境中的手动回归。)_
