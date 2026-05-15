## MODIFIED Requirements

### Requirement: 管理页删除 UI

每条剧集的删除按钮 SHALL 出现在两处：
1. 剧详情页 (`/admin/dramas/{slug}`) 的集列表表格中每一行（除非该集状态为 `encoding`）。
2. 单集详情页 (`/admin/dramas/{slug}/episodes/{ep}`) 的页面操作区。

点击删除按钮 SHALL 弹出浏览器 `confirm()` 对话框进行二次确认；确认后前端 SHALL 向 `DELETE /admin/episodes/{slug}/{ep}` 发起请求。

成功（HTTP 200）后的导航行为：
- **从剧详情页触发**：调用页面刷新（reload 或重新拉取 `/admin/dramas/{slug}/full` 并重渲染集列表），从 UI 中移除该行。
- **从单集详情页触发**：浏览器导航回 `/admin/dramas/{slug}`。

旧版的单页扁平列表 (`/admin` 上的剧集表格中的删除按钮) 已被 `admin-redesign` 替换为剧目卡片首页，不再承载剧集级删除操作；该旧 UI 路径已不存在。

#### Scenario: 从剧详情页删除一集
- **GIVEN** 用户打开 `/admin/dramas/ly`，该剧有集 1、2、3，状态分别为 ready、encoding、ready
- **THEN** 集 1 和集 3 的行有"删除"按钮；集 2 的行没有（因为 status=encoding）
- **WHEN** 用户点击集 3 行的删除按钮并在 `confirm()` 中确认
- **THEN** 前端发起 `DELETE /admin/episodes/ly/3`
- **AND** 收到 200 后页面刷新（或局部重渲染）
- **AND** 集 3 的行从表格中消失

#### Scenario: 从单集详情页删除并跳转回剧详情
- **GIVEN** 用户打开 `/admin/dramas/ly/episodes/3`
- **WHEN** 用户点击"[删除本集]"按钮并确认
- **THEN** 前端发起 `DELETE /admin/episodes/ly/3`
- **AND** 收到 200 后浏览器导航到 `/admin/dramas/ly`

#### Scenario: 用户取消二次确认不发起请求
- **WHEN** 用户点击删除按钮但在 `confirm()` 对话框点"取消"
- **THEN** 前端 MUST NOT 发起 DELETE 请求
- **AND** 页面保持不变
