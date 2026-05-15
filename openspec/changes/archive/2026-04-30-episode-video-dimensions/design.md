## Context

SDK 的 `media3-shortdrama` 在渲染容器构建阶段需要预知视频原始宽高，否则首帧到达时会触发 `SurfaceView` resize → 视觉抖动。当前 `EpisodeInfo` 没透传这一对，客户端只能：

- 拿到 m3u8 → 准备播放器 → 等 ExoPlayer 出 `Tracks.Format.width/height` 事件 → resize SurfaceView

这条等待路径让首帧后的容器调整成为可见动作。把宽高放进 `EpisodeInfo` payload 后，UI 在拉到接口的瞬间就能定容器，与 cover 渲染同步出去，零抖动。

服务端做这件事的成本接近零：上传时本来就 ffprobe 一次取 duration，多取两个字段是 free。

## Goals / Non-Goals

**Goals**：

- 新上传的 ready 行 `EpisodeInfo.width` / `.height` 含正确的源视频原始 codec width / height（与 SDK `Format.width/height` 同口径）。
- 老 ready 行透明退化（两个字段为 `null`）；客户端发现 null 时仍走"等首帧"老路径。
- 不破坏 `episode-info-schema.json` 的兼容性（新增可选字段，老 SDK 忽略即可）。
- 不动 pipeline / 编码 ladder。

**Non-Goals**：

- 不做老行回探迁移 —— 源 mp4 已删，回探得不到原始分辨率，且没有清晰的"在哪一个本地产物上探"的目标。
- 不返回 display dimension（SAR 拉伸后的"显示"尺寸）。短剧场景 SAR 几乎都是 1:1，引入 display 尺寸只增字段不增价值。
- 不返回各档 ladder（540p / 720p / 1080p）的渲染宽高 —— 这些可由源高度 + 固定 ladder 比例线性推出，给客户端无好处。
- 不引入 frame rate / codec / bitrate / SAR 等其它 ffprobe 字段 —— 单一目的（容器尺寸预定）够用。

## Decisions

### D1. `width` / `height` 取 codec dimension，不取 display dimension

**决策**：`ffprobe -select_streams v:0 -show_entries stream=width,height` —— 这是 codec 像素分辨率，与 ExoPlayer `Format.width/height` 同源。

**理由**：

- SDK 客户端的 `Format.width/height` 拿到的就是 codec dimension；服务端给同一口径的值，客户端拿到就能直接用、不做换算。
- Display dimension（含 SAR 校正）需要乘 `sample_aspect_ratio` 做整数舍入，多一层 trapdoor，且短剧 SAR 通常 1:1（不用算）。
- 真要做 display：在客户端用 `Format.pixelWidthHeightRatio` 做一次乘法即可，零侵入。

**替代方案**：

- 取 display dimension：见上，弃。
- 同时返回 codec + display 两套：四个字段，浪费载荷，弃。

### D2. 字段名 `width` / `height`，不带前缀

**决策**：JSON / Pydantic / DB 列都用 `width` / `height`。

**理由**：

- 短而清晰；EpisodeInfo 的语境本身已经限定是"视频字段"，不会与"封面图宽高"混淆（cover 没单独的尺寸字段）。
- SDK 端代码读 `episodeInfo.width` 比 `episodeInfo.videoWidth` 简洁。
- 与 `durationMs` 的简洁口径一致（不叫 `videoDurationMs`）。

**替代方案**：

- `videoWidth` / `videoHeight`：更明确但冗长，弃。
- `sourceWidth` / `sourceHeight`：暗示源 vs 渲染区分，但本 change 决定只暴露源（D1），无歧义，弃。

### D3. 字段类型 `["integer", "null"]`，不进 `required`

**决策**：schema 里两个字段用 `{"type": ["integer", "null"], "minimum": 1}`，不放进 `required` 数组。

**理由**：

- 老 ready 行的源 mp4 已删，无法回探，DB 里两列只能 NULL。
- 老 SDK（不识别 width/height）忽略未知字段，无影响。
- 新 SDK 看到 null 时退化到"等首帧"行为；看到值就预定容器。两条路径都明确。
- `ge=1`（pydantic）/ `minimum=1`（schema）拒绝 0 / 负值，保护 SDK 假设。

**替代方案**：

- 必填：要么强制重传所有老剧集（操作员负担大），要么塞个 sentinel 值（破坏语义），都不可接受。
- 字段缺省（`additionalProperties: false` 下从响应里直接省略）：会让 SDK 老版本先看到字段、新版本看到 null、再上一次升级又拿到值，状态空间多一档。统一返回 null 简洁。

### D4. DB ALTER 在 `init_db` 里做，幂等

**决策**：`init_db()` 在 `executescript(_SCHEMA)` 之后做：

```python
for col in ("width", "height"):
    try:
        conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} INTEGER")
    except sqlite3.OperationalError:
        pass  # 列已存在
```

**理由**：

- SQLite 3.35+ 支持 `ALTER TABLE ADD COLUMN IF NOT EXISTS`，但项目最低 SQLite 版本不确定（macOS 自带可能更旧）—— 用 try/except OperationalError 兼容更稳。
- 启动时执行，保证 lifespan 第一次接客户端请求前 schema 一致。
- 幂等：重启多次不破坏数据。

**替代方案**：

- 单独写迁移脚本：内部小服务 + 单 SQLite 文件，过度工程。
- 直接 `DROP TABLE + CREATE TABLE`：丢数据，弃。

### D5. ffprobe 调用单独一次，不复用 `probe_duration_ms`

**决策**：新增 `probe_video_dimensions(src) -> tuple[int, int]`，与 `probe_duration_ms` 平级。上传 handler 串行调两次。

**理由**：

- 单次 ffprobe 取多 entries 是可行（`-show_entries format=duration:stream=width,height -of json`），但解析路径要分支处理（duration 在 format、宽高在 stream）；为节省一次 ffprobe 调用而引入 JSON 解析得不偿失。
- 两次 ffprobe 都很快（< 50ms 每次），上传是用户行为，非热点。
- 函数职责单一更易测、易复用 —— 未来若有"按需重新探测"场景，函数可独立调用。

**替代方案**：

- 一次 ffprobe + JSON 解析：复杂度上升，速度提升微乎其微，弃。
- 在 `extract_first_frame` 里一并产出宽高：两件事耦合，弃。

### D6. width / height 探测失败 → 上传 400

**决策**：`probe_video_dimensions` 失败时，上传 handler 与 `probe_duration_ms` 失败一致 —— `tmp_path.unlink` + 抛 `HTTPException(400, "ffprobe failed: ...")`。不允许 fallback 到 NULL 继续走 pipeline。

**理由**：

- 探测失败说明源文件破损或 ffprobe 异常 —— 接下来的 ffmpeg 转码大概率也会挂；早 fail 早醒目。
- 一致性：duration 探测失败拒收，宽高探测失败也拒收，规则简洁。
- 不让用户在不知情的情况下得到 width=null 的新上传 ready 行（会让 null 语义双义：老行 vs 新行探测失败，运维难分辨）。

**替代方案**：

- 失败降级到 NULL 继续走：见上，弃。
- 仅警告日志：客户端拿不到值，运维也排查不到原因，弃。

## Risks / Trade-offs

- **[Risk] ffprobe 多探一次延迟**：< 50ms，相对几十秒到几分钟的整个上传 + 转码流程可忽略。
- **[Risk] 无法回探老数据**：老 ready 行的 SDK fast-start 仍走"等首帧"路径，两套代码并存。**Mitigation**：客户端 SDK 应保留对 null 的退路；本 change 不强制升级。
- **[Trade-off] 不做 display dimension**：短剧场景 SAR ≈ 1:1，client 端按需做一次乘法即可（D1）。
- **[Trade-off] 字段不放进 `required`**：兼容老行的代价是 SDK 客户端代码必须处理 null 分支；可接受，因为客户端反正要兼容老接口。

## Migration Plan

零迁移：

1. 升级到含本 change 的服务版本。
2. lifespan 启动时 `init_db` 自动 ALTER 两列。
3. 重启完成；既有 SDK 端点继续工作；管理页继续工作。
4. 新上传的 ready 行返回真实 width / height；老 ready 行返回 null。

回滚：把代码降级即可。新加的两列在老代码里被忽略（`SELECT *` 拿到但 `dict(row)` 多两个键，下游路径不消费），不破坏。

## Open Questions

- 是否需要在管理页 `/admin` 表格里展示一列 "宽×高"？短期不展示（运维不关心，只看 status / 时长）；真有需求再扩。
- 客户端是否要在 `Format.pixelWidthHeightRatio != 1.0` 时优先信 codec width/height + 拉伸 vs 信 SAR-corrected display？属于 SDK 渲染策略，不是服务端契约面，由 SDK 团队决定。
