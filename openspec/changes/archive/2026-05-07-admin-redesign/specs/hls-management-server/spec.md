## REMOVED Requirements

### Requirement: Upload intake

**Reason**: Replaced by the per-drama episode upload endpoints (`POST /admin/dramas/{slug}/episodes` for auto-increment and `POST /admin/dramas/{slug}/episodes/{ep}` for re-upload) introduced by the `admin-redesign` capability. The legacy `POST /admin/upload` endpoint is removed because (a) the new endpoints carry the drama slug in the URL rather than the form, (b) episode-number assignment is server-driven for new uploads, and (c) re-upload semantics are explicit at the path level.

**Migration**: Replace any callers of `POST /admin/upload` (drama_slug, ep_number, video) with one of:
- New episode: `POST /admin/dramas/{drama_slug}/episodes` with `video` only.
- Re-upload an existing episode: `POST /admin/dramas/{drama_slug}/episodes/{ep_number}` with `video` only.

The `pipeline.sh` invocation contract, the persistence schema for `episodes`, the DRM key endpoint, and the SDK episode-info endpoint are unchanged.

## MODIFIED Requirements

### Requirement: Admin web page

The service SHALL serve `GET /admin` returning an HTML page rendered against the shared admin base layout (see the `admin-redesign` capability's "shared admin layout and navigation" requirement). The page SHALL display a grid of drama cards as defined by the `admin-redesign` capability's "drama cards homepage" requirement, plus a "+ 创建短剧" call-to-action linking to `/admin/dramas/new`.

The page SHALL NOT contain a free-text upload form. Episode uploads now happen on the per-drama detail page (`/admin/dramas/{slug}`) via the auto-increment endpoint, and on the per-episode detail page for re-uploads.

The legacy two-form layout (drama-create + episode-upload + flat episode list) introduced in `drama-as-entity` is replaced wholesale by this new layout.

#### Scenario: Admin page is the drama cards homepage
- **WHEN** the client requests `GET /admin`
- **THEN** the response is 200 HTML extending the shared admin base layout
- **AND** the body contains drama cards (one per drama) and a "+ 创建短剧" link to `/admin/dramas/new`
- **AND** the body does NOT contain a `<form action="/admin/upload">` element

#### Scenario: Root redirects to admin
- **WHEN** the client requests `GET /`
- **THEN** the response is a 302/307 redirect to `/admin`
