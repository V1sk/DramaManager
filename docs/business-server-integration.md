# 业务服务器对接文档

本文档描述 **HLS 管理服务器**（本仓库 / staging 服务器）与**业务服务器**（独立部署 / prod）之间的同步协议。

---

## 1. 系统职责划分

| 职责 | HLS 管理服务器（本服务） | 业务服务器（你们这边） |
|---|---|---|
| 视频上传 / 切片 / DRM 加密（FFmpeg + AES-128-CBC） | ✅ | ❌ |
| 剧 / 集 / 翻译 / 标签 / 演员 / 字幕的录入和编辑 | ✅ | ❌ |
| 操作员管理后台（`/admin/...`） | ✅ | ❌ |
| OSS staging 前缀（`Drama/staging/...`）的写入和清理 | ✅ | ❌ |
| OSS prod 前缀（`Drama/prod/...`）的拷贝和清理 | ✅（被动，由 `/sync/*` 触发） | ❌（只读消费） |
| 持久化剧 / 集 / DRM 数据，对外提供 SDK API | ❌ | ✅ |
| 客户端 SDK（`media3-shortdrama` Android）流量入口 | ❌ | ✅ |
| 海报 / 封面 / 字幕字节的本地存储 | 临时存（直到同步） | ✅（最终态） |
| `/drm/{slug}/ep-{n}/key` 端点对客户端提供 16-byte AES key | ❌ | ✅ |

**核心交互**：操作员在 HLS 服务器编辑完成后**手动**点击"同步"按钮，HLS 服务器调用业务服务器的 `/sync/*` 端点把状态推送过去。同步过程是单 worker FIFO 队列，全程异步，操作员通过状态徽章（dirty / syncing / clean / sync_failed / pending_delete）观察进度。

---

## 2. 整体拓扑

```
┌────────────────────────────┐         ┌────────────────────────────┐
│   HLS 管理服务器 (staging) │         │   业务服务器 (prod)        │
│                            │         │                            │
│  - /admin/* 操作员后台     │  POST   │  - /sync/* 接收同步         │ ←── 你们实现
│  - /api/* (调试用，可选)   │ ──────► │  - /api/* SDK 流量入口     │ ←── 你们实现
│  - /drm/* (本地预览用)     │  /sync/ │  - /drm/* SDK 取 AES key   │ ←── 你们实现
│  - /videos/* (静态切片)    │  调用   │                            │
│                            │         │                            │
└──────────┬─────────────────┘         └──────────┬─────────────────┘
           │                                      │
           │ 写 staging                           │ 读 prod
           │ Drama/staging/...                    │ Drama/prod/...
           │                                      │
           ▼                                      ▼
        ┌──────────────────────────────────────────┐
        │  阿里云 OSS (photobundle bucket)          │
        │   Drama/staging/{slug}/{ep_dir}/...      │
        │   Drama/prod/{slug}/{ep_dir}/...         │
        └──────────────────────────────────────────┘
```

- **OSS bucket 共享**。staging 和 prod 共用 `photobundle` bucket、共用一套凭证；只是路径前缀不同。
- **同步即"OSS 内服务端拷贝 + HTTP 通知"**。HLS 服务器在调你们 `/sync/episodes` 之前，会先用 `bucket.copy_object` 把 `Drama/staging/{...}` 拷一份到 `Drama/prod/{...}`（不下载到本地、不重传）。
- **m3u8 文本由 HTTP body 传给你们**，不通过 OSS。你们写到自己的本地磁盘 / OSS / CDN 都行。
- **海报 / 封面 / 字幕字节**：HLS 端给的是 URL（`/videos/...` 路径或绝对 URL），你们在收到 sync 请求时**同步从 HLS 服务器拉取**。

---

## 3. 鉴权

每个 `/sync/*` 请求带 header：

```
X-API-Key: <共享密钥>
```

- 密钥在两侧用环境变量配置：HLS 端 `BUSINESS_SYNC_API_KEY`，业务端命名自定（推荐同名）。
- 密钥不匹配 → 业务服务器返回 **401**。
- HLS 端不会重试 401；操作员会看到红色 `sync_failed` 徽章 + 错误信息，需要他们去检查配置。

部署建议：通过 secrets manager / env vault 注入；轮换时两边同时滚动。

---

## 4. `/sync/*` 协议（4 个端点）

所有请求 / 响应都是 `application/json`（DELETE 端点除外，无 body）。

### 4.1 `POST /sync/dramas` — upsert 一部剧

**请求 body**：

```json
{
  "slug": "ly",
  "default_lang": "zh-rCN",
  "client_updated_at": "2026-05-07T12:34:56Z",
  "translations": {
    "zh-rCN": {
      "name": "琅琊榜",
      "synopsis": "豪门复仇 ...",
      "poster_url": "/videos/ly/poster/zh-rCN.jpg"
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
| `slug` | str | 剧 slug，匹配 `^[a-z0-9][a-z0-9-]*$`，主键 |
| `default_lang` | str | 默认语言 code（必须出现在 `languages` 里） |
| `client_updated_at` | str | ISO 8601 UTC，HLS 端的 `dramas.updated_at`。**用于乱序保护**：如果业务服务器侧已存的 `client_updated_at` 比这个新 → 返回 **409** |
| `translations` | object | 按 `lang_code` 索引；每个值的 `name` 必填，`synopsis` 和 `poster_url` 可空 |
| `translations[lang].poster_url` | str ∣ null | **相对路径**（`/videos/...`），业务服务器需要拼上 HLS 服务器 host 后**主动拉取**字节 |
| `tags` | array | 该剧关联的全部标签；每个 tag 包含其全部语言翻译 |
| `actors` | array | 同上，演员 |
| `languages` | array | 该剧涉及的全部语言（drama 翻译 ∪ tag 翻译 ∪ actor 翻译 ∪ 字幕语言的并集） |

**处理流程**（业务服务器 SHALL 按顺序执行）：

1. 验证 `X-API-Key`，不匹配 → 401。
2. 检查 `client_updated_at`：如果 DB 里已有更新的版本 → 409。
3. 对每个 **非空** `poster_url` 同步拉取字节（拼接 HLS 服务器 host 后 GET），任一失败 → **502** 并在响应体里指明失败的 URL。
4. Upsert `languages` 行（idempotent on `code`）。
5. Upsert `tags` 行 + tag 翻译。
6. Upsert `actors` 行 + actor 翻译。
7. Upsert `dramas` 行 + drama 翻译。
8. 把 poster 字节写到本地 / 永久存储，路径建议 `<biz_OUT_DIR>/{slug}/poster/{lang}.<ext>`（扩展名按 Content-Type 决定）。

**响应**：

| 状态码 | body | 含义 |
|---|---|---|
| `200` | `{"ok": true, "client_updated_at": "...", "synced_at": "..."}` | 同步成功 |
| `401` | `{"error": "..."}` | API key 不匹配 |
| `409` | `{"error": "stale payload", "stored_client_updated_at": "..."}` | 乱序保护命中 |
| `502` | `{"error": "...", "failing_url": "..."}` | 拉海报字节失败 |
| `4xx/5xx` | `{"error": "..."}` | 其它错误，错误字符串会回显在 HLS 端 `sync_error` 列里 |

---

### 4.2 `DELETE /sync/dramas/{slug}` — 删除一部剧

无 body。

**处理流程**：

1. 验证 `X-API-Key`，不匹配 → 401。
2. 删除该剧的所有持久化数据：drama 行、translations、tag-drama 关联、actor-drama 关联、所有集（episodes 行 + 落盘的 m3u8 / 字幕 / 封面 / DRM key）、海报字节。
3. **幂等**：剧不存在也返回 204（不要 404）。

**响应**：

| 状态码 | 含义 |
|---|---|
| `204` | 删除成功（或目标本就不存在） |
| `401` | API key 不匹配 |

HLS 端在收到 2xx 后会**额外**调用 `unpublish_drama_from_prod(slug)` 把 OSS prod 前缀下属于这部剧的所有切片对象删掉。所以你们**不需要**触碰 OSS。

---

### 4.3 `POST /sync/episodes` — upsert 一集

**前置条件**：必须先调过该剧的 `POST /sync/dramas` 至少一次，否则 HLS 端不会发出这个请求（HLS 端拦在 409）。

**请求 body**：

```json
{
  "drama_slug": "ly",
  "ep_number": 3,
  "episode_id": "ly-ep-3",
  "client_updated_at": "2026-05-07T12:34:56Z",
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
  "cover_url": "/videos/ly/ep-3/cover.jpg",
  "subtitles": [
    {
      "lang_code": "en",
      "label": "English",
      "url": "/videos/ly/ep-3/subtitles/en.vtt"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `drama_slug` / `ep_number` | str / int | 联合定位一集；`episode_id` 是 `"{drama_slug}-ep-{ep_number}"`，由 HLS 端生成，verbatim 透传 |
| `client_updated_at` | str | 该集 `episodes.updated_at`，乱序保护，规则同 drama |
| `duration_ms` | int | 视频时长（毫秒），FFmpeg 探测 |
| `width` / `height` | int ∣ null | 源视频分辨率，与 SDK `Format.width/height` 同口径；老数据可能是 null（升级前已存在的行） |
| `drm.key_uri` | str | **相对路径** `/drm/{slug}/ep-{n}/key`。业务服务器需要在自己的 host 上**实现这个端点**，返回 16 字节 raw 二进制；播放器按 m3u8 自身 host 解析这条 URI |
| `drm.key_base64` | str | 16 字节 AES key 的 base64。`base64.b64decode(key_base64)` MUST 得到正好 16 字节 |
| `drm.iv_hex` | str ∣ null | 32 字符的 hex IV。可空（播放器从 m3u8 的 `#EXT-X-KEY:IV` fallback） |
| `playlists.{ladder}` | str | **完整的 m3u8 文本**，三档 540p / 720p / 1080p 都有。`#EXT-X-MAP:URI` 和 segment 行是**绝对的 OSS prod URL**（`https://photobundle.oss-ap-southeast-1.aliyuncs.com/Drama/prod/...`）。`#EXT-X-KEY:URI` 是 `/drm/{slug}/ep-{n}/key` 相对路径 |
| `cover_url` | str | **相对路径**（`/videos/...`），需要业务服务器主动拉字节 |
| `subtitles[].url` | str | 同上，每条字幕是 WebVTT 文件 |

**处理流程**（业务服务器 SHALL 按顺序执行）：

1. 验证 `X-API-Key`，不匹配 → 401。
2. 检查该剧是否已存在（前置同步过）：找不到 → **409** `{"error": "drama not synced first"}`。
3. 检查 `client_updated_at` 乱序 → 409。
4. **同步拉取** `cover_url` 和每条 `subtitles[].url` 的字节，任一失败 → 502 并指明失败的 URL。
5. 解码 `drm.key_base64` 得 16 字节，写入业务服务器自己的 keys 目录（推荐 `<biz_OUT_DIR>/{slug}/keys/ep-{n}.key`）。
6. 把每档 ladder 的 m3u8 文本写到 `<biz_OUT_DIR>/{slug}/ep-{n}/{ladder}/media-{ladder}.m3u8`。
7. 把 cover 字节写到 `<biz_OUT_DIR>/{slug}/ep-{n}/cover.jpg`。
8. 把每条字幕字节写到 `<biz_OUT_DIR>/{slug}/ep-{n}/subtitles/{lang}.vtt`。
9. Upsert `episodes` 行（drama_slug, ep_number 联合主键）。

**注意**：m3u8 里引用的 `init-{ladder}.mp4` 和 `seg-{ladder}-N.m4s` 已经由 HLS 端**通过 OSS server-side copy** 放在了 `Drama/prod/{slug}/ep-{n}/{ladder}/` 下。**你们什么都不用做** —— 客户端按 m3u8 里的绝对 OSS URL 直接走 OSS（CDN）拿。

**响应**：

| 状态码 | body | 含义 |
|---|---|---|
| `200` | `{"ok": true, "client_updated_at": "...", "synced_at": "..."}` | 同步成功 |
| `401` | `{"error": "..."}` | API key 不匹配 |
| `409` | `{"error": "drama not synced first"}` 或 `{"error": "stale payload"}` | 前置缺失或乱序 |
| `502` | `{"error": "...", "failing_url": "..."}` | 拉 cover / 字幕字节失败 |

---

### 4.4 `DELETE /sync/episodes/{slug}/{ep}` — 删除一集

无 body。

**处理流程**：

1. 验证 `X-API-Key`，不匹配 → 401。
2. 删除该集的本地数据：episode 行、m3u8 文件、字幕、封面、DRM key 文件。
3. **不**删除剧行（即使是该剧最后一集）；剧的删除由 `DELETE /sync/dramas/{slug}` 单独触发。
4. **幂等**：集不存在也返回 204。

**响应**：

| 状态码 | 含义 |
|---|---|
| `204` | 删除成功（或目标本就不存在） |
| `401` | API key 不匹配 |

HLS 端在收到 2xx 后会调用 `unpublish_ladder_from_prod(slug, ep_dir, ladder)` 三次（三档）把 OSS prod 端切片删掉。

---

## 5. URL 拉取 contract

业务服务器在收到 `/sync/dramas` 或 `/sync/episodes` 时需要主动从 HLS 服务器拉取以下文件字节：

| 字段 | 内容 | 拼接规则 |
|---|---|---|
| `translations[lang].poster_url` | 海报图（jpg/png/webp） | `<HLS_HOST>{poster_url}` |
| `cover_url` | 集封面（jpg） | `<HLS_HOST>{cover_url}` |
| `subtitles[].url` | WebVTT 字幕 | `<HLS_HOST>{url}` |

`<HLS_HOST>` 的来源建议：

- **方式 A**（推荐）：业务服务器侧配一个 env `HLS_STAGING_HOST=https://hls-internal.example.com`，每次拼上去。
- **方式 B**：HLS 端在请求 body 里多带一个 `staging_host` 字段（**未实现**，需要协议升级）。

**网络要求**：业务服务器要能 HTTP 访问 HLS 服务器（同 VPN / 内网）。HLS 服务器侧没有鉴权（部署在 VPN 内网），业务服务器直接 GET 即可。

**Content-Type 处理**：

- 海报：`image/jpeg` / `image/png` / `image/webp`，按响应头决定写盘扩展名。
- 封面：固定 `image/jpeg`。
- 字幕：`text/vtt`，写盘时确认开头是 `WEBVTT` 魔术字（HLS 端已校验）。

**失败语义**：拉取失败（HTTP ≠ 2xx 或网络错误）→ 整个 sync 请求返回 502，HLS 端会把错误标记到 `sync_failed`，操作员重试时业务服务器收到的还是同一个 payload（幂等重试安全）。

---

## 6. DRM 密钥处理

### 6.1 业务服务器需要实现 `GET /drm/{slug}/{ep_dir}/key`

- 入参：路径 `slug` / `ep_dir`（形如 `ep-3`）。
- 出参：**16 字节 raw binary**，`Content-Type: application/octet-stream`。
- 鉴权：暂未要求（按 SDK 部署形态决定；如果客户端走业务服务器认证，可在这里检查会话 token）。

### 6.2 关键 verbatim 契约

m3u8 里的 `#EXT-X-KEY:URI="/drm/{slug}/ep-{n}/key"` 与业务服务器实际暴露的 `/drm/{slug}/ep-{n}/key` 路径**必须字节级一致**。播放器（ExoPlayer / hls.js）会按 m3u8 自身的 host 解析这条相对 URI，因此该 host 必须由业务服务器提供。

### 6.3 16 字节校验

落盘 key 文件之前请校验：

```python
key = base64.b64decode(payload["drm"]["key_base64"])
assert len(key) == 16, f"invalid AES key length: {len(key)}"
```

### 6.4 IV 处理

- `iv_hex` 非空 → 32 字符 hex，对应 16 字节 IV。播放器解密时用这个。
- `iv_hex` 为 null → 播放器从 m3u8 的 `#EXT-X-KEY:IV` 字段 fallback。

业务服务器无须解密；DRM key + IV 是给客户端的，业务服务器只是把它们按客户端的 fetch 路径准备好。

---

## 7. OSS 访问

### 7.1 OSS 凭证

目前 staging 和 prod 共用 bucket `photobundle`、共用一套 access key（硬编码在 HLS 端 `app/oss_upload.py`）。业务服务器**只读** prod 前缀，不需要 access key —— 所有 prod 端 m3u8 / .mp4 / .m4s 都在 OSS bucket 公开可读，按绝对 URL 直接 GET。

如果未来要把 prod 切到独立 bucket / 独立凭证 / 私有 ACL，需要新开 spec 升级协议。

### 7.2 OSS CORS

OSS bucket CORS 必须允许 GET 来自客户端 host 的 Origin（浏览器播放器走 hls.js 时需要 CORS；ExoPlayer / iOS 原生不走 CORS）。Staging 和 prod 共用 bucket 共用 CORS。

如果 SDK 客户端通过业务服务器域名播放，CORS 规则里需要加业务服务器的 Origin。

### 7.3 OSS 路径常量（信息）

| 常量 | 值 |
|---|---|
| Bucket | `photobundle` |
| Endpoint | `https://oss-ap-southeast-1.aliyuncs.com` |
| Public base | `https://photobundle.oss-ap-southeast-1.aliyuncs.com` |
| Staging 前缀 | `Drama/staging/{slug}/{ep_dir}/{ladder}/` |
| Prod 前缀 | `Drama/prod/{slug}/{ep_dir}/{ladder}/` |
| Init 文件名约定 | `init-{ladder}.mp4` |
| Segment 文件名约定 | `seg-{ladder}-{N}.m4s` |

---

## 8. 业务服务器对外的 SDK API（建议形态）

HLS 端的 `/api/*` 端点是给本地调试用的；**业务服务器需要实现自己的同形 SDK API**，因为：
- 客户端只跟业务服务器通信（HLS 服务器在内网）；
- m3u8 引用的 `Drama/prod/...` URL 只有业务服务器同步过来的数据才会指向它；
- 区域 / 用户 / 鉴权 / 并发 / 限流策略是业务服务器侧的。

建议至少实现：

| 端点 | 用途 |
|---|---|
| `GET /api/dramas` | 剧目录（`DramaSummary[]`） |
| `GET /api/dramas/{slug}/episodes` | 一部剧的全部 ready 集（`EpisodeInfo[]`） |
| `GET /api/episodes/{slug}/{ep}` | 单集的 `EpisodeInfo` |
| `GET /api/languages` | 可用语言列表（按 SDK 字幕选择器消费） |
| `GET /api/tags` | 标签列表（带 `?lang=` 解析 —— 这是 follow-up 的 `sdk-search-and-localization` capability） |
| `GET /api/actors` | 演员列表（同上） |

`EpisodeInfo` schema 见 HLS 仓库的 [`episode-info-schema.json`](../episode-info-schema.json)。**关键字段**：

- `playUrl`：业务服务器侧建议指向自己的 m3u8 路径（不是 HLS 端的 `/videos/...`）。
- `coverUrl` / `posterUrl`：业务服务器自己 host 的相对路径。
- `initUrl` / `firstSegUrl`：可以是 OSS prod 绝对 URL（client warmup 用）。
- `drm.keyUri`：业务服务器侧 `/drm/{slug}/ep-{n}/key`，与 m3u8 里的 `#EXT-X-KEY:URI` 字节级一致。
- `drm.keyBase64`：可选 —— SDK 用 `keyUri` 主动拉，或者在 `EpisodeInfo` 里嵌 `keyBase64` 直接预热（看你们设计）。
- `subtitles`：每条 `{langCode, label, url}`，url 指向业务服务器 host 上的 vtt 文件。

---

## 9. 状态机（HLS 端的同步状态，仅供参考）

业务服务器**不需要**关心 HLS 端的状态机；这部分仅说明 HLS 端如何观察同步结果，方便排错。

```
dirty   ──► syncing ──► clean
              │
              └──► sync_failed
pending_delete ──► syncing ──► (HLS 端的行物理删除)
                       │
                       └──► sync_failed
```

- HLS 端调你们的 `/sync/*` 后，根据返回码：
  - **2xx** → HLS 端把行翻成 `clean`（或在 delete 流程里物理删除该行）。
  - **非 2xx** → HLS 端把行翻成 `sync_failed`，错误回显在管理后台徽章上。
- HLS 端**没有**自动重试。操作员看到红色徽章后会点"重试"，重新发同样的 payload。
- 因此你们的 `/sync/*` 必须**幂等**：同一个 payload 重复调用，结果应该和调一次一样。

---

## 10. 部署 / 网络

### 10.1 网络

- HLS 服务器 → 业务服务器：HTTP `/sync/*` 调用，建议同 VPN / 同 VPC。
- 业务服务器 → HLS 服务器：HTTP GET 拉 cover / poster / 字幕字节。
- 业务服务器 → 阿里云 OSS：用不到（除非你们想把 prod 切片镜像到自己的 CDN）。
- 客户端 → 业务服务器：SDK API + DRM key + （如果走 hls.js）m3u8。
- 客户端 → 阿里云 OSS：m3u8 引用的 `Drama/prod/...` 绝对 URL，直连或经 CDN。

### 10.2 业务服务器侧的环境变量（建议）

| 变量 | 作用 |
|---|---|
| `BUSINESS_SYNC_API_KEY` | 与 HLS 端共享的 API key；从 `X-API-Key` 取出来比对 |
| `HLS_STAGING_HOST` | 拉海报 / 封面 / 字幕字节时的 host（`https://hls-internal.example.com`） |
| `BIZ_OUT_DIR` | 落盘根目录（m3u8 / cover / subtitle / DRM key 文件） |
| `BIZ_DB_PATH` | 业务侧 DB（持久化 dramas / episodes 行） |

---

## 11. 错误码总结

| HTTP 码 | 出现场景 | HLS 端如何呈现 |
|---|---|---|
| 200 / 204 | 成功 | 行 → `clean`（或物理删除） |
| 401 | `X-API-Key` 不匹配 | 行 → `sync_failed`，错误："业务 sync HTTP 401: ..." |
| 409 | `client_updated_at` 乱序 / 剧未先同步 | 行 → `sync_failed`，操作员一般通过先点"同步整部剧"解决 |
| 502 | 业务服务器拉海报 / 封面 / 字幕字节失败 | 行 → `sync_failed`，错误体里指明失败的 URL |
| 5xx 其它 | 业务服务器内部错误 | 行 → `sync_failed`，错误字符串截断到 ~512 字符回显 |

---

## 12. 端到端示例时序

操作员上传一集 + 同步整部剧的完整链路：

```
操作员                HLS 服务器              业务服务器               OSS
  │                      │                        │                    │
  │ 上传 mp4             │                        │                    │
  ├─────────────────────►│                        │                    │
  │                      │ pipeline.sh 切片+加密  │                    │
  │                      │ publish_ladder × 3     │                    │
  │                      ├──────────────────────────────────────────►  │ Drama/staging/...
  │                      │ episode 行 sync_status=dirty                │
  │                      │                        │                    │
  │ 点击"[同步整部剧]"   │                        │                    │
  ├─────────────────────►│                        │                    │
  │                      │ POST /sync/dramas      │                    │
  │                      ├───────────────────────►│                    │
  │                      │                        │ GET poster_url     │
  │                      │◄───────────────────────┤ (拉海报字节)       │
  │                      ├───────────────────────►│                    │
  │                      │   200 OK               │                    │
  │                      │◄───────────────────────┤                    │
  │                      │ drama 行 sync_status=clean                  │
  │                      │                        │                    │
  │                      │ publish_ladder_to_prod × 3                  │
  │                      ├──────────────────────────────────────────►  │ Drama/prod/...
  │                      │   (server-side copy, 不走本机网卡)          │
  │                      │                        │                    │
  │                      │ POST /sync/episodes    │                    │
  │                      ├───────────────────────►│ (含 prod m3u8 文本)│
  │                      │                        │ GET cover_url       │
  │                      │◄───────────────────────┤ GET subtitle_url[*] │
  │                      ├───────────────────────►│ (拉字节)           │
  │                      │   200 OK               │                    │
  │                      │◄───────────────────────┤                    │
  │                      │ episode 行 sync_status=clean                │
  │                      │                        │                    │
  │ 徽章变绿 (clean)     │                        │                    │
  │◄─────────────────────┤                        │                    │
  │                                                                    │
  │ ......（一段时间后，客户端来访问）                                 │
  │                                                                    │
客户端                                          业务服务器              OSS
  │                                                │                    │
  │ GET /api/episodes/ly/3                         │                    │
  ├───────────────────────────────────────────────►│                    │
  │   EpisodeInfo (含 playUrl, drm 等)             │                    │
  │◄───────────────────────────────────────────────┤                    │
  │                                                │                    │
  │ GET m3u8                                       │                    │
  ├───────────────────────────────────────────────►│                    │
  │ GET /drm/ly/ep-3/key (16 bytes)                │                    │
  ├───────────────────────────────────────────────►│                    │
  │                                                │                    │
  │ GET init-720p.mp4                              │                    │
  ├──────────────────────────────────────────────────────────────────► │ (Drama/prod/)
  │ GET seg-720p-N.m4s × N                         │                    │
  ├──────────────────────────────────────────────────────────────────► │
  │                                                │                    │
  │ AES-128-CBC 解密 → 播放                                              │
```

---

## 13. 协议变更历史

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-07 | 首版：4 个 `/sync/*` 端点 + URL 拉取 contract + 双 OSS 前缀 |

未来变更建议走 OpenSpec change（`openspec/changes/` 下新建一个 change，proposal 描述协议改动，业务服务器侧同步升级）。
