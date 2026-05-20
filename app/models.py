from typing import List, Optional

from pydantic import BaseModel, Field


class DrmInfo(BaseModel):
    keyUri: str
    keyBase64: str = Field(pattern=r"^[A-Za-z0-9+/=]{24}$")
    ivHex: Optional[str] = Field(default=None, pattern=r"^[0-9A-Fa-f]{32}$")


class FallbackPlaylists(BaseModel):
    low: Optional[str] = None
    high: Optional[str] = None


class Subtitle(BaseModel):
    """Side-loaded subtitle track. SDK clients fetch the URL and attach it
    to the player as a text track at attach time (no `#EXT-X-MEDIA` in the
    HLS playlist — out-of-band by design).

    `mimeType` is always `text/vtt` — every subtitle on disk is WebVTT
    (single upload only accepts `.vtt`; batch upload converts `.srt` →
    WebVTT). The field is emitted so SDK clients don't have to infer it.
    """

    langCode: str
    label: str
    url: str
    mimeType: str = "text/vtt"


class EpisodeInfo(BaseModel):
    episodeId: str
    playUrl: str
    durationMs: int = Field(ge=0)
    coverUrl: Optional[str] = None
    width: Optional[int] = Field(default=None, ge=1)
    height: Optional[int] = Field(default=None, ge=1)
    initUrl: Optional[str] = None
    firstSegUrl: Optional[str] = None
    drm: Optional[DrmInfo] = None
    fallback: Optional[FallbackPlaylists] = None
    subtitles: Optional[List[Subtitle]] = None


class DramaSummary(BaseModel):
    dramaSlug: str
    dramaName: str
    epCount: int = Field(ge=1)
    latestEpNumber: int = Field(ge=1)
    posterUrl: Optional[str] = None
    lastUpdatedAt: str


class AdminEpisode(BaseModel):
    drama_slug: str
    drama_name: str
    ep_number: int
    episode_id: str
    status: str
    duration_ms: Optional[int] = None
    play_url: Optional[str] = None
    cover_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
