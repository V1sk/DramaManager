## MODIFIED Requirements

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
