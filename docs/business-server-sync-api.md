# 业务服务器同步 API（简明版）

HLS 管理服务器（staging）通过 4 个 HTTP 接口把剧 / 集 / 翻译 / 标签 / 演员 / 字幕 / 封面 / 海报 / DRM 推送给业务服务器（prod）。媒体字节走 TOS server-side copy，业务服务器只接 JSON。

> 长版本（含拓扑图 / 状态机 / 升级说明 / FAQ）见 `docs/business-server-integration.md`。本文档只列协议本身。

---

## 通用

| 项 | 值 |
|---|---|
| Content-Type | `application/json`（DELETE 无 body） |
| 鉴权 | 每个请求带 `X-API-Key: <共享密钥>`；不匹配 → **401** |
| 编码 | UTF-8 |
| 时间戳 | ISO 8601 UTC，例如 `2026-05-19T01:23:45Z` |
| TOS bucket | `coocent-drama` @ `tos-ap-southeast-1.volces.com`（HLS 端写 prod，业务端只读 prod） |
| TOS prod 前缀 | `Drama/prod/{slug}/...` |

**乱序保护**：所有 upsert 请求带 `client_updated_at`；业务服务器对比 DB 里已存的版本，若收到的 `client_updated_at` ≤ 已存的 → 返回 **409** `{"error":"stale payload","stored_client_updated_at":"..."}`，HLS 端不会自动重试，由操作员手动触发。

**幂等**：所有 DELETE 端点对"目标不存在"返回 **204**（不要 404）。

**错误回显**：业务服务器 ≥400 响应的 body（≤512 字节）会原样落到 HLS 端 `sync_error` 列，操作员鼠标 hover 红色徽章能看到。

---

## 1. `POST /sync/dramas` — upsert 一部剧

**Body**：

```json
{
  "slug": "ly",
  "default_lang": "zh-rCN",
  "free_episodes": 3,
  "client_updated_at": "2026-05-19T01:23:45Z",
  "translations": {
    "zh-rCN": {
      "name": "琅琊榜",
      "synopsis": "豪门复仇 ...",
      "poster_url": "https://coocent-drama.tos-ap-southeast-1.volces.com/Drama/prod/ly/poster/zh-rCN.jpg"
    },
    "en": {
      "name": "Langya Bang",
      "synopsis": null,
      "poster_url": null
    }
  },
  "tags": [
    {
      "slug": "urban",
      "default_lang": "zh-rCN",
      "translations": { "zh-rCN": "都市", "en": "Urban" }
    }
  ],
  "actors": [
    {
      "slug": "zhang-san",
      "default_lang": "zh-rCN",
      "translations": { "zh-rCN": "张三", "en": "Zhang San" }
    }
  ],
  "languages": [
    { "code": "zh-rCN", "display_label": "简体中文" },
    { "code": "en",     "display_label": "English"  }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `slug` | str | 剧主键，`^[a-z0-9][a-z0-9-]*$` |
| `default_lang` | str | 默认语言 code（必须在 `languages` 里） |
| `free_episodes` | int | 前 N 集免费，第 N+1 集起付费；`0` = 全部付费 |
| `client_updated_at` | str | HLS 端 `dramas.updated_at`，乱序保护 |
| `translations` | object | 按 `lang_code` 索引；`name` 必填，`synopsis` / `poster_url` 可空 |
| `translations[lang].poster_url` | str ∣ null | **绝对 TOS prod URL**；业务端 opaque 存储，**不要 GET** |
| `tags[]` / `actors[]` | array | 各自含 `slug` + `default_lang` + 全部语言翻译 |
| `languages[]` | array | 该剧涉及的全部语言（drama / tag / actor / 字幕语言的并集） |

**处理顺序**：upsert `languages` → `tags` → `actors` → `dramas` + 翻译。`poster_url` 是不透明字符串，存进去给 SDK 透传即可。

**响应**：

| 状态码 | Body | 含义 |
|---|---|---|
| `200` | `{"ok":true,"client_updated_at":"...","synced_at":"..."}` | 同步成功 |
| `401` | `{"error":"..."}` | API key 不匹配 |
| `409` | `{"error":"stale payload","stored_client_updated_at":"..."}` | 乱序保护命中 |
| `4xx`/`5xx` | `{"error":"..."}` | 其它错误（HLS 端 sync_failed） |

---

## 2. `DELETE /sync/dramas/{slug}` — 删一部剧

无 body。

**处理**：删除该剧全部本地数据（drama 行、translations、tag/actor 关联、所有 episodes 行 + m3u8 / 字幕 / 封面 / DRM key）。

**响应**：

| 状态码 | 含义 |
|---|---|
| `204` | 删除成功（或本就不存在） |
| `401` | API key 不匹配 |

> HLS 端在收到 2xx 后会自动调 TOS `unpublish_drama_from_prod(slug)` 扫除该剧 prod 前缀全部对象，**业务端不需要触碰 TOS**。

---

## 3. `POST /sync/episodes` — upsert 一集

**前置**：该剧必须先成功同步过 `POST /sync/dramas` 至少一次，否则 HLS 端不会发出本请求；如果业务端先收到 episode 请求但找不到剧 → 返回 **409** `{"error":"drama not synced first"}`。

**Body**：

```json
{
  "drama_slug": "ly",
  "ep_number": 3,
  "episode_id": "ly-ep-3",
  "client_updated_at": "2026-05-19T01:23:45Z",
  "duration_ms": 150000,
  "width": 720,
  "height": 1280,
  "drm": {
    "key_uri": "/drm/ly/ep-3/key",
    "key_base64": "QUJDREVGR0hJSktMTU5PUA==",
    "iv_hex": "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
  },
  "playlists": {
    "540p":  "#EXTM3U\n#EXT-X-VERSION:7\n...",
    "720p":  "#EXTM3U\n#EXT-X-VERSION:7\n...",
    "1080p": "#EXTM3U\n#EXT-X-VERSION:7\n..."
  },
  "cover_url": "https://coocent-drama.tos-ap-southeast-1.volces.com/Drama/prod/ly/ep-3/cover.jpg",
  "subtitles": [
    {
      "lang_code": "en",
      "label": "English",
      "url": "https://coocent-drama.tos-ap-southeast-1.volces.com/Drama/prod/ly/ep-3/subtitles/en.vtt"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `drama_slug` / `ep_number` | str / int | 联合主键 |
| `episode_id` | str | `"{drama_slug}-ep-{ep_number}"`，HLS 端生成，verbatim 透传给 SDK |
| `client_updated_at` | str | `episodes.updated_at`，乱序保护 |
| `duration_ms` | int | FFmpeg 探测的视频时长 |
| `width` / `height` | int ∣ null | 源视频 codec 分辨率；老数据可能为 null |
| `drm.key_uri` | str | **相对路径** `/drm/{slug}/ep-{n}/key`；与 SDK `EpisodeInfo.drm.keyUri` verbatim 一致 |
| `drm.key_base64` | str | base64 编码的 16 字节 AES key；`base64.b64decode` 后 MUST 恰好 16 字节 |
| `drm.iv_hex` | str ∣ null | 32 字符 hex IV；可空（播放器从 m3u8 `#EXT-X-KEY:IV` fallback） |
| `playlists.{ladder}` | str | 三档 540p / 720p / 1080p 完整 m3u8 文本；`#EXT-X-MAP:URI` + segment 行已经是**绝对 TOS prod URL**，`#EXT-X-KEY:URI` 是 `/drm/...` 相对路径 |
| `cover_url` | str | **绝对 TOS prod URL**，opaque 存储 |
| `subtitles[].url` | str | 同上 |

**处理顺序**：
1. 验证该剧已 upsert 过 → 否则 409。
2. 检查 `client_updated_at` 乱序 → 否则 409。
3. base64 解码 `drm.key_base64` 写到 keys 目录（建议 `<biz_out>/{slug}/keys/ep-{n}.key`）。
4. 三档 m3u8 文本写到 `<biz_out>/{slug}/ep-{n}/{ladder}/media-{ladder}.m3u8`。
5. Upsert `episodes` 行，把 `cover_url` / `subtitles[].url` 作为 opaque 字符串存进去。

> m3u8 引用的 `init-*.mp4` / `seg-*.m4s`、payload 里的 `cover_url` / `subtitles[].url` 已经由 HLS 端通过 TOS server-side copy 放在 prod 前缀；**业务端不需要任何 TOS 出站**，客户端按绝对 URL 直连 TOS。

**响应**：

| 状态码 | Body | 含义 |
|---|---|---|
| `200` | `{"ok":true,"client_updated_at":"...","synced_at":"..."}` | 同步成功 |
| `401` | `{"error":"..."}` | API key 不匹配 |
| `409` | `{"error":"drama not synced first"}` ∣ `{"error":"stale payload",...}` | 前置缺失或乱序 |

---

## 4. `DELETE /sync/episodes/{slug}/{ep}` — 删一集

无 body。`{ep}` 是纯数字（无 `ep-` 前缀）。

**处理**：删除该集本地数据（episodes 行 + m3u8 + 字幕 + 封面 + DRM key 文件）。**不**级联删剧。

**响应**：

| 状态码 | 含义 |
|---|---|
| `204` | 删除成功（或本就不存在） |
| `401` | API key 不匹配 |

> HLS 端在收到 2xx 后会自动调 TOS `unpublish_episode_from_prod(slug, ep_dir)` 扫除该集 prod 前缀全部对象（三档 ladder + cover + 字幕）。

---

## 5. 业务端必须实现的 DRM 端点：`GET /drm/{slug}/{ep_dir}/key`

业务端给客户端怎么暴露剧 / 集 / 字幕等数据是你们自己的事（自由组织 API），但 **DRM key 端点是 sync 协议强制约束** —— 因为 m3u8 里 `#EXT-X-KEY:URI` 是 `/drm/{slug}/ep-{n}/key` 相对路径，播放器按 m3u8 自身 host 解析这条 URI，所以这个 host 必须由业务端提供并实现：

- `{ep_dir}` 形如 `ep-3`（带 `ep-` 前缀）。
- 返回：**16 字节 raw binary**，`Content-Type: application/octet-stream`。
- URL 必须与 m3u8 里 `#EXT-X-KEY:URI` **字节级一致**。
- key 字节本身从 `POST /sync/episodes` 的 `drm.key_base64` 解码而来（解码后 MUST 是 16 字节）。

---

## 6. 失败 & 重试

- 业务端 ≥400 / 网络超时 → HLS 端 row 置 `sync_failed`，红色徽章 + 错误文本。
- **没有自动重试**：操作员点"重试本集"或"同步整部剧"再触发一次。
- 同步顺序：drama 先同步（更新 `last_synced_at`），再串行同步该剧名下全部 dirty / pending_delete 集。
- `pending_delete` 状态：HLS 端"删除一集"后行不立刻消失，等同步 `DELETE /sync/episodes/{slug}/{ep}` 收到 2xx 才物理删行 + 清 TOS prod。
