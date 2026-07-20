## Why

运营分类当前依赖上下按钮逐项调整顺序，剧目较多时操作次数多且难以直观看到目标位置。改为通过明确的拖拽手柄实时排序，可以让运营人员直接把剧目移动到期望位置。

## What Changes

- 在推荐、最新、最热、独家模块的每个剧目卡片上增加排序拖拽手柄。
- 操作员按住手柄后可以上下拖动卡片，列表在拖动过程中实时重排，并在放下后持久化最终顺序。
- 拖拽期间提供清晰的移动状态和放置位置反馈。
- 保留无鼠标场景可用的排序方式，避免降低键盘和触屏操作能力。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `featured-category-curation`: 将运营分类页面的排序要求从上下移动按钮扩展为支持通过拖拽手柄实时重排并持久化。

## Impact

- 主要影响 `app/templates/featured_categories.html` 的列表结构、样式和客户端交互逻辑。
- 复用现有 `PUT /admin/featured-categories/{category}` 接口，不改变数据库结构或业务服务器同步协议。
- 更新运营分类页面测试，覆盖拖拽手柄、拖拽事件和持久化行为。
