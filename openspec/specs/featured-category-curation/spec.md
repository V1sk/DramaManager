# featured-category-curation Specification

## Purpose

定义后台对推荐、最新、最热、独家四个固定运营分类的有序编排、独立发布状态和业务服务器同步行为，确保运营人员能够选剧、移除、拖拽排序，并以完整有序快照安全发布到业务服务器。

## Requirements
### Requirement: fixed featured categories are ordered collections

The service SHALL define exactly four featured categories in canonical display order: `recommend`（推荐）, `new`（最新）, `hot`（最热）, and `exclusive`（独家）. Each category SHALL contain an ordered list of distinct existing drama slugs. A drama MAY belong to multiple categories.

#### Scenario: a drama belongs to multiple ordered categories
- **GIVEN** dramas `a`, `b`, and `c` exist
- **WHEN** `recommend` is stored as `[b, a]` and `hot` is stored as `[c, b]`
- **THEN** reading the categories returns those arrays in exactly that order
- **AND** drama `b` belongs to both categories

#### Scenario: deleting a drama removes its category memberships
- **GIVEN** drama `b` belongs to `recommend` and `hot`
- **WHEN** drama `b` is physically deleted
- **THEN** its memberships are removed by foreign-key cascade
- **AND** the remaining members retain their relative order

### Requirement: existing featured memberships migrate to ordered storage

`init_db()` SHALL idempotently migrate a legacy `drama_featured_categories` table that has no `sort_order` column to the new schema. It SHALL preserve every existing relationship, accept the new `recommend` value, and assign each legacy category a deterministic contiguous order.

#### Scenario: legacy table is migrated
- **GIVEN** an existing database whose featured table contains `hot` memberships and has no `sort_order`
- **WHEN** `init_db()` runs
- **THEN** every prior membership still exists
- **AND** `sort_order` values within `hot` are contiguous starting at zero
- **AND** running `init_db()` again leaves the data unchanged

### Requirement: featured category management API

The service SHALL provide `GET /admin/featured-categories.json` returning the four categories with ordered drama summaries plus the complete selectable drama list. It SHALL provide `PUT /admin/featured-categories/{category}` accepting JSON `{"drama_slugs": [str, ...]}` and atomically replacing that category in the supplied order. Unknown categories, malformed bodies, invalid slugs, and missing dramas SHALL be rejected without changing existing membership.

#### Scenario: replace category members and order
- **GIVEN** `recommend` currently contains `[a, b]` and dramas `a`, `b`, `c` exist
- **WHEN** the client sends `{"drama_slugs": ["c", "a"]}` to `PUT /admin/featured-categories/recommend`
- **THEN** the response is 200 and `recommend` is exactly `[c, a]`
- **AND** drama `b` is no longer a member

#### Scenario: missing drama makes replacement atomic
- **GIVEN** `hot` currently contains `[a, b]`
- **WHEN** the client attempts to replace it with `[b, missing]`
- **THEN** the response is 404
- **AND** `hot` remains `[a, b]`

#### Scenario: unknown category is rejected
- **WHEN** the client sends a replacement to `/admin/featured-categories/seasonal`
- **THEN** the response is 404 or 422
- **AND** no category changes

### Requirement: drama-level category editor preserves collection order

The existing `PUT /admin/dramas/{slug}/featured-categories` endpoint SHALL continue to replace one drama's category membership set. Removing membership SHALL preserve the relative order of remaining dramas. Adding membership SHALL append that drama to the end of the affected category. Category changes SHALL NOT mark the drama content sync status dirty because category publication uses its dedicated sync action.

#### Scenario: drama is appended from detail editor
- **GIVEN** `recommend` contains `[a, b]` and drama `c` is not a member
- **WHEN** the drama-level endpoint assigns `c` to `recommend`
- **THEN** `recommend` becomes `[a, b, c]`
- **AND** `c.sync_status` is unchanged

#### Scenario: drama is removed without reordering others
- **GIVEN** `hot` contains `[a, b, c]`
- **WHEN** the drama-level endpoint removes `b` from `hot`
- **THEN** `hot` becomes `[a, c]`

### Requirement: featured category operations page

`GET /admin/featured-categories` SHALL render four category modules. Each module SHALL expose a control that opens a modal listing every drama with the category's current members selected. Operators SHALL be able to select or deselect dramas, save the selection, remove an individual drama directly, and reorder members through a dedicated drag handle. While the handle is dragged vertically, the list SHALL reflect the prospective order in real time, and cards SHALL animate smoothly between exchanged positions unless the user prefers reduced motion. The final order SHALL be persisted when the handle is released. The drag handle SHALL also provide a keyboard-accessible ordering mechanism. A failed reorder save SHALL restore the previously persisted order.

#### Scenario: operator selects dramas in modal then drags to sort them
- **GIVEN** the operator opens the 推荐 module's drama picker
- **WHEN** they select dramas `a` and `b` and save
- **THEN** both dramas appear in 推荐
- **WHEN** they hold `b`'s sort handle and drag it above `a`
- **THEN** the displayed list moves `b` above `a` during the drag
- **AND** releasing the handle persists 推荐 as `[b, a]`

#### Scenario: operator sorts with the keyboard
- **GIVEN** 推荐 contains `[a, b]`
- **WHEN** the operator focuses `b`'s sort handle and presses the upward ordering key
- **THEN** 推荐 is persisted as `[b, a]`

#### Scenario: exchanged cards animate into place
- **GIVEN** 推荐 contains `[a, b, c]`
- **AND** the operator has not enabled reduced motion
- **WHEN** dragging `a` causes it to exchange positions with `b`
- **THEN** the affected cards animate from their previous visual positions into the new positions
- **AND** a later exchange during the same drag starts from the cards' current visual positions without an abrupt jump

#### Scenario: reduced motion disables exchange animation
- **GIVEN** the operator prefers reduced motion
- **WHEN** a drag changes the prospective order
- **THEN** the cards move to the new order without a transition animation

#### Scenario: DOM reordering does not end an active drag
- **GIVEN** 推荐 contains `[a, b, c]`
- **WHEN** the operator keeps the pointer pressed and drags `a` across `b` toward `c`
- **THEN** moving `a` past `b` SHALL NOT end or cancel the drag
- **AND** the operator can continue moving `a` past `c` before releasing the pointer
- **AND** the final displayed order is persisted only after the actual pointer release

#### Scenario: failed reorder restores prior order
- **GIVEN** 推荐 is persisted as `[a, b]`
- **WHEN** the operator drags `b` above `a` and the replacement request fails
- **THEN** the displayed 推荐 order returns to `[a, b]`

#### Scenario: operator removes a drama directly
- **GIVEN** 最新 contains `[a, b]`
- **WHEN** the operator clicks remove on `a`
- **THEN** 最新 is persisted as `[b]`

### Requirement: featured category sync button reflects unsynced changes

The service SHALL persist whether the featured-category snapshot has actual local changes since its last successful dedicated sync. A category mutation that changes ordered membership, or physical deletion of a categorized drama, SHALL mark the snapshot dirty. Replacing a category or a drama's memberships with an identical effective value SHALL preserve the current clean state. A successful dedicated sync SHALL mark it clean; a failed sync SHALL leave it dirty.

The operations page SHALL render the “同步运营分类” button with its primary highlighted style only while this state is dirty. While clean, the button SHALL use a non-highlighted secondary style and SHALL remain available for an explicit repeat sync when business sync is configured.

#### Scenario: actual edit highlights the sync action
- **GIVEN** the featured-category snapshot is clean
- **WHEN** an operator adds, removes, or reorders a drama
- **THEN** the snapshot becomes dirty
- **AND** the operations page renders the sync button highlighted

#### Scenario: identical replacement does not highlight
- **GIVEN** the snapshot is clean and 推荐 is `[a, b]`
- **WHEN** an operator saves 推荐 as `[a, b]` again
- **THEN** the snapshot remains clean
- **AND** the sync button remains non-highlighted

#### Scenario: successful sync clears highlight
- **GIVEN** the snapshot is dirty
- **WHEN** the dedicated business sync succeeds
- **THEN** the snapshot becomes clean
- **AND** the sync button changes to its non-highlighted style

#### Scenario: failed sync preserves highlight
- **GIVEN** the snapshot is dirty
- **WHEN** the business server rejects the dedicated sync or the request fails
- **THEN** the snapshot remains dirty
- **AND** the sync button remains highlighted
