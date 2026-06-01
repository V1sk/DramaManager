"""Ladder rung ↔ SDK `videoTracks` identity — the single source of truth shared
by the SDK API (`routers/api.py`) and the business-server sync payload
(`sync.py`).

Keeping both consumers on this one table guarantees the rung `id` mapping and
the per-rung `width` / `height` derivation are byte-identical wherever they
surface: the `EpisodeInfo.videoTracks` the SDK reads, and the `video_tracks`
array the sync worker ships to the business server.

`LADDER_TRACKS` must stay in sync with the `LADDERS` array in `pipeline.sh` and
with `episode-info-schema.json`'s `videoTracks[].id` enum (high / mid / low).
Ordered high → low so every consumer emits descending quality identically.
"""

# (track_id, ladder, rung_height) — `rung_height` is the fixed `scale=-2:HEIGHT`
# height the pipeline encodes each rung at.
LADDER_TRACKS = (
    ("high", "1080p", 1080),
    ("mid", "720p", 720),
    ("low", "540p", 540),
)


def rung_dimensions(src_w, src_h, rung_height):
    """Encoded (width, height) of one ladder rung. `encode-clear.sh` runs
    `scale=-2:HEIGHT`, so the rung height is fixed and the width follows the
    source aspect ratio rounded to an even integer. Returns (None, None) when
    the source dimensions are unknown (legacy rows)."""
    if not src_w or not src_h:
        return None, None
    width = int(src_w * rung_height / src_h / 2 + 0.5) * 2
    return max(width, 2), rung_height
