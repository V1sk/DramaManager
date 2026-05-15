## MODIFIED Requirements

### Requirement: 剧目录查询

服务 SHALL 提供 `GET /api/dramas` 端点，返回 JSON 数组 `DramaSummary[]`，其中每个元素对应 **存在于 `dramas` 表且至少有一集 `status='ready'`** 的剧。没有任何符合条件的剧时服务 SHALL 返回 `200 []`。

`DramaSummary` 结构（字段名、类型、可空性是 SDK 契约，**不变**；本要求仅改变 `dramaName` 的取值来源）：

| 字段 | 类型 | 来源 |
|---|---|---|
| `dramaSlug` | `string` | `dramas.slug` |
| `dramaName` | `string` | `translations.value` 中匹配 `(entity_type='drama', entity_id=dramas.slug, lang_code=dramas.default_lang, field='name')` 的行；通过 JOIN 取 |
| `epCount` | `integer ≥ 1` | 该剧 `status=ready` 的 `episodes` 行数 |
| `latestEpNumber` | `integer ≥ 1` | 该剧 `status=ready` 行的最大 `ep_number` |
| `posterUrl` | `string \| null` | 见下方"封面选取规则" |
| `lastUpdatedAt` | `string` | 该剧全部 ready 行里 `updated_at` 的最大值，ISO 8601 |

`dramaName` 的取值来源从 `dramas.name` 列改为 `translations` 表的默认语言行；wire-format（字段名、类型、字符串值）byte-identical（同一部剧用同一份 default_lang 名称返回相同字符串）。

注意：`drama-meta-translations` 之后 SDK 仍只看到 default-lang 名称；多语言协商（`?lang=`）由 `sdk-search-and-localization` (step 6) 引入。

**封面选取规则**（不变）：`posterUrl` 等于该剧 ready 状态集里 **`ep_number` 最小** 的那一集的 `cover_url`。注意：drama-level 多语言海报（drama-meta-translations 引入的 `OUT_DIR/{slug}/poster/{lang_code}.*`）此版本中**尚未** 用于 `DramaSummary.posterUrl`。SDK 端 drama-level 海报的暴露由 `sdk-search-and-localization` 接手。

**排序**（不变）：数组 SHALL 按 `lastUpdatedAt` 降序排列。两行 `lastUpdatedAt` 相同时 SHALL 按 `dramaSlug` 升序作为次级排序。

#### Scenario: dramaName 取自 translations 表的 default_lang 行
- **GIVEN** `dramas` 中 `slug='ly', default_lang='zh-rCN'`，`translations` 中存在 `('drama', 'ly', 'zh-rCN', 'name', '琅琊榜')`，`episodes` 中至少一集 ready
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 对应元素的 `dramaName='琅琊榜'`
- **AND** 该值通过 JOIN `translations` 取得，不依赖 `dramas` 表上任何 `name` 列

#### Scenario: 没有 default_lang name 翻译的剧仍返回（带空字符串或 fallback）
- **GIVEN** 数据库异常状态：`dramas` 中 `slug='broken', default_lang='zh-rCN'`，`translations` 中没有对应 name 行（理论上 POST `/admin/dramas` 阻止了这种状态出现，但作为防御）
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 该剧的 `dramaName` 为空字符串 `""`（既不让响应失败也不让该剧消失）

#### Scenario: 没有任何 ready 集的剧不出现在目录中
- **GIVEN** `dramas` 中存在 `slug='newcoming'`，所有相关 `episodes` 行状态为 `encoding` 或 `failed`
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应数组中不包含 `dramaSlug=newcoming` 的条目

#### Scenario: 全库无 ready 记录返回空数组
- **GIVEN** `episodes` 表中没有任何状态为 `ready` 的行
- **WHEN** 客户端请求 `GET /api/dramas`
- **THEN** 响应为 200，响应体为 `[]`
