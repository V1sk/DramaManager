## MODIFIED Requirements

### Requirement: 剧目录查询

服务 SHALL 提供 `GET /api/dramas` 端点，返回 JSON 数组 `DramaSummary[]`，其中每个元素对应 **存在于 `dramas` 表且至少有一集 `status='ready'`** 的剧。没有任何符合条件的剧时服务 SHALL 返回 `200 []`。

`DramaSummary` 结构（字段名、类型、可空性是 SDK 契约，不变）：

| 字段 | 类型 | 来源 |
|---|---|---|
| `dramaSlug` | `string` | `dramas.slug` |
| `dramaName` | `string` | `dramas.name`（通过 JOIN 取，不再是 `MAX(episodes.drama_name)`） |
| `epCount` | `integer ≥ 1` | 该剧 `status=ready` 的 `episodes` 行数 |
| `latestEpNumber` | `integer ≥ 1` | 该剧 `status=ready` 行的最大 `ep_number` |
| `posterUrl` | `string \| null` | 见下方"封面选取规则" |
| `lastUpdatedAt` | `string` | 该剧全部 ready 行里 `updated_at` 的最大值，ISO 8601 |

**封面选取规则**（不变）：`posterUrl` 等于该剧 ready 状态集里 **`ep_number` 最小** 的那一集的 `cover_url`（通常是第 1 集；若第 1 集未 ready，则退化到 ready 集里最小编号的那一集）。若最小编号那一集的 `cover_url` 为 NULL，`posterUrl` 为 `null`。

**排序**（不变）：数组 SHALL 按 `lastUpdatedAt` 降序排列。两行 `lastUpdatedAt` 相同时 SHALL 按 `dramaSlug` 升序作为次级排序。

注意：`dramas` 表里存在但没有任何 `status=ready` 集的剧不会出现在该端点输出中（保持 SDK 只看到"可播"的剧的契约）。

#### Scenario: 多部剧按最近更新时间降序返回
- **GIVEN** `dramas` 表中存在三部剧 `a`、`b`、`c`，`episodes` 中：
  - `a`：2 集 ready（`lastUpdatedAt = 2026-04-20T10:00:00Z`）
  - `b`：3 集 ready（`lastUpdatedAt = 2026-04-24T10:00:00Z`）
  - `c`：1 集 ready（`lastUpdatedAt = 2026-04-22T10:00:00Z`）
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，数组顺序是 `[b, c, a]`
- **AND** 每项都包含 `dramaSlug`、`dramaName`、`epCount`、`latestEpNumber`、`posterUrl`、`lastUpdatedAt` 六个字段

#### Scenario: 没有任何 ready 集的剧不出现在目录中
- **GIVEN** `dramas` 中存在 `slug='newcoming'`，所有相关 `episodes` 行状态为 `encoding` 或 `failed`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应数组中不包含 `dramaSlug=newcoming` 的条目

#### Scenario: 全库无 ready 记录返回空数组
- **GIVEN** `episodes` 表中没有任何状态为 `ready` 的行
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，响应体为 `[]`

#### Scenario: 没有任何剧集的空 dramas 行不出现
- **GIVEN** `dramas` 中存在 `slug='empty'`，`episodes` 中没有任何对应行
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应数组中不包含 `dramaSlug=empty` 的条目

#### Scenario: dramaName 取自 dramas.name
- **GIVEN** `dramas` 中 `slug='ly', name='琅琊榜'`，`episodes` 中至少一集 ready
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 对应元素的 `dramaName='琅琊榜'`，且这个值 100% 来自 `dramas.name`，不依赖 episode 行

#### Scenario: 封面取最小 ready ep_number 的 cover
- **GIVEN** `dramas` 中存在 `slug='x'`，`episodes` 中 `ep_number=1` 状态 `failed`、`ep_number=2` 状态 `ready` 且 `cover_url='/videos/x/ep-2/cover.jpg'`、`ep_number=3` 状态 `ready` 且 `cover_url='/videos/x/ep-3/cover.jpg'`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 对应 `dramaSlug='x'` 的元素里 `posterUrl = '/videos/x/ep-2/cover.jpg'`
