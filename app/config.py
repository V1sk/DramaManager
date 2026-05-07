import os
from dataclasses import dataclass
from pathlib import Path


# Ladder rungs the pipeline always produces. Keep in sync with `LADDERS` in pipeline.sh.
ALLOWED_LADDERS = ("540p", "720p", "1080p")


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    db_path: Path
    upload_tmp_dir: Path
    pipeline_script: Path
    oss_enabled: bool
    # Which ladder rung the SDK / preview player should consume by default.
    # Affects EpisodeInfo.playUrl / initUrl / firstSegUrl at read time. Flipping
    # this env var takes effect on the next API read — no re-encoding needed.
    # Useful for ad-hoc debugging (e.g. force 540p to test bandwidth-constrained
    # client behavior without re-encoding the source).
    default_ladder: str
    # business-server-sync (step 6): when `business_sync_base_url` is unset,
    # sync is disabled — admin sync endpoints return 503 and the sync UI is
    # hidden. When set, `business_sync_api_key` MUST also be set (validated at
    # startup); the worker calls into `<base>/sync/*` with `X-API-Key`.
    business_sync_base_url: str | None
    business_sync_api_key: str | None
    business_sync_timeout: int


def _parse_bool_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"true", "1", "yes"}


def load_settings() -> Settings:
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = Path(os.environ.get("OUT_DIR", repo_root / "out")).resolve()
    db_path = Path(os.environ.get("DB_PATH", repo_root / "hls.db")).resolve()
    tmp_dir = Path(os.environ.get("UPLOAD_TMP_DIR", repo_root / "tmp")).resolve()

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    default_ladder = os.environ.get("DEFAULT_LADDER", "720p").strip()
    if default_ladder not in ALLOWED_LADDERS:
        raise RuntimeError(
            f"DEFAULT_LADDER must be one of {ALLOWED_LADDERS}, got {default_ladder!r}"
        )

    sync_base = (os.environ.get("BUSINESS_SYNC_BASE_URL", "") or "").strip() or None
    sync_key = (os.environ.get("BUSINESS_SYNC_API_KEY", "") or "").strip() or None
    if sync_base is not None and sync_base.endswith("/"):
        # Strip trailing slash so url joins are predictable; httpx.AsyncClient
        # handles base_url with or without trailing slash, but downstream
        # f-strings would double-slash without this.
        sync_base = sync_base.rstrip("/")
    if sync_base is not None and sync_key is None:
        raise RuntimeError(
            "BUSINESS_SYNC_BASE_URL is set but BUSINESS_SYNC_API_KEY is not. "
            "Both must be set together to enable business-server sync."
        )
    sync_timeout_raw = os.environ.get("BUSINESS_SYNC_TIMEOUT", "30").strip()
    try:
        sync_timeout = int(sync_timeout_raw) if sync_timeout_raw else 30
    except ValueError as e:
        raise RuntimeError(
            f"BUSINESS_SYNC_TIMEOUT must be an integer (seconds), got {sync_timeout_raw!r}"
        ) from e
    if sync_timeout <= 0:
        raise RuntimeError(
            f"BUSINESS_SYNC_TIMEOUT must be positive, got {sync_timeout}"
        )

    return Settings(
        out_dir=out_dir,
        db_path=db_path,
        upload_tmp_dir=tmp_dir,
        pipeline_script=(repo_root / "pipeline.sh").resolve(),
        oss_enabled=_parse_bool_env("OSS_ENABLED"),
        default_ladder=default_ladder,
        business_sync_base_url=sync_base,
        business_sync_api_key=sync_key,
        business_sync_timeout=sync_timeout,
    )


settings = load_settings()
