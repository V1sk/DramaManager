## 1. Ordered Featured Category Storage

- [x] 1.1 Extend the featured-category schema and constants with `recommend`, `sort_order`, and the category/order index.
- [x] 1.2 Add an idempotent legacy table rebuild that preserves memberships and assigns deterministic contiguous positions.
- [x] 1.3 Implement category-level ordered replacement/read helpers and adapt drama-level membership replacement to append/remove without disturbing other members.

## 2. Admin APIs and Page

- [x] 2.1 Add featured-category JSON read and ordered replacement endpoints with atomic validation.
- [x] 2.2 Rebuild the operations page with four category modules, the selectable drama modal, direct removal, and up/down ordering controls.
- [x] 2.3 Keep the drama detail category editor compatible with all four categories and stop marking drama content dirty for category-only edits.

## 3. Dedicated Business Sync

- [x] 3.1 Add a complete ordered featured-category snapshot builder and remove the legacy category field from drama sync payloads.
- [x] 3.2 Add the permission-protected HLS sync endpoint that calls business `PUT /sync/featured-categories` and maps disabled/upstream failures.
- [x] 3.3 Add the explicit sync control and result feedback to the featured-category operations page.

## 4. Verification

- [x] 4.1 Add database tests for four-category storage, ordered replacement, drama-level append/remove behavior, validation atomicity, and legacy migration.
- [x] 4.2 Add sync tests for the dedicated payload/endpoint behavior and absence of featured categories from drama payloads.
- [x] 4.3 Run OpenSpec validation, focused tests, the full pytest suite, and `git diff --check`.

## 5. Featured Category Sync Highlight State

- [x] 5.1 Add persistent featured-category dirty/clean state and mark it only on effective category changes or categorized-drama deletion.
- [x] 5.2 Clear dirty state only after a successful dedicated sync and render the sync button primary only while dirty.
- [x] 5.3 Add dirty-state regression tests and rerun OpenSpec validation, focused tests, the full suite, and `git diff --check`.
