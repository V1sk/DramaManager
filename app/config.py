import os
from dataclasses import dataclass
from pathlib import Path


# Ladder rungs the pipeline always produces. Keep in sync with `LADDERS` in pipeline.sh.
ALLOWED_LADDERS = ("540p", "720p", "1080p")

# Object-storage backends `STORAGE_PROVIDER` may select.
ALLOWED_STORAGE_PROVIDERS = ("none", "oss", "tos")


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    db_path: Path
    upload_tmp_dir: Path
    pipeline_script: Path
    # True iff a cloud bucket provider is selected (i.e. storage_provider != "none").
    # Renamed from `oss_enabled` once Volcengine TOS landed alongside Aliyun OSS;
    # the conceptual flag is "do we upload to a bucket" rather than "which vendor".
    storage_enabled: bool
    # Which bucket provider to use. `"oss"` = Aliyun OSS; `"tos"` = Volcengine TOS;
    # `"none"` = bucket disabled, everything stays on local disk.
    storage_provider: str
    # Which ladder rung the admin preview player consumes by default. The SDK
    # EpisodeInfo carries all three rungs (videoTracks) and is unaffected; this
    # only rewrites the admin-facing play_url at read time (see
    # db._apply_default_ladder). Flipping the env var takes effect on the next
    # read — no re-encoding needed. Useful for ad-hoc debugging (e.g. force 540p
    # to test bandwidth-constrained client behavior without re-encoding).
    default_ladder: str
    # business-server-sync (step 6): when `business_sync_base_url` is unset,
    # sync is disabled — admin sync endpoints return 503 and the sync UI is
    # hidden. When set, `business_sync_api_key` MUST also be set (validated at
    # startup); the worker calls into `<base>/sync/*` with `X-API-Key`.
    business_sync_base_url: str | None
    business_sync_api_key: str | None
    business_sync_timeout: int
    # How many pipeline jobs (encode + encrypt + OSS publish) run concurrently.
    # Each job is its own `pipeline.sh` subprocess; ffmpeg is already
    # multi-threaded so this oversubscribes CPU — 2 is a sane default, raise
    # only if the box has spare cores. Same-episode jobs are still serialized
    # by a per-episode lock in queue.py.
    pipeline_concurrency: int
    # admin-accounts-auth: secret used to sign the `/admin` session cookie.
    # REQUIRED — `load_settings()` fails fast if unset, because a per-boot
    # random key would silently invalidate every session on each restart.
    session_secret_key: str
    # admin-accounts-auth: password for the bootstrap `admin` account, only
    # consumed by `init_db()` on first boot when the `users` table is empty.
    # Optional here; `init_db()` fails fast if it is needed and unset.
    admin_initial_password: str | None


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

    sync_base = (os.environ.get("BUSINESS_SYNC_BASE_URL", "http://127.0.0.1:9000") or "").strip() or None
    sync_key = (os.environ.get("BUSINESS_SYNC_API_KEY", "demo-secret-key") or "").strip() or None
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

    # STORAGE_PROVIDER selects the bucket backend. OSS_ENABLED=true is kept as
    # a back-compat alias so existing deployments don't need to change env.
    # Precedence: explicit STORAGE_PROVIDER wins; if unset, fall back to
    # interpreting OSS_ENABLED.
    storage_raw = os.environ.get("STORAGE_PROVIDER", "tos").strip().lower()
    legacy_oss_enabled = _parse_bool_env("OSS_ENABLED")
    if not storage_raw:
        storage_provider = "oss" if legacy_oss_enabled else "none"
    else:
        storage_provider = storage_raw
    if storage_provider not in ALLOWED_STORAGE_PROVIDERS:
        raise RuntimeError(
            f"STORAGE_PROVIDER must be one of {ALLOWED_STORAGE_PROVIDERS}, "
            f"got {storage_provider!r}"
        )
    storage_enabled = storage_provider != "none"

    concurrency_raw = os.environ.get("PIPELINE_CONCURRENCY", "2").strip()
    try:
        pipeline_concurrency = int(concurrency_raw) if concurrency_raw else 2
    except ValueError as e:
        raise RuntimeError(
            f"PIPELINE_CONCURRENCY must be an integer, got {concurrency_raw!r}"
        ) from e
    if pipeline_concurrency < 1:
        raise RuntimeError(
            f"PIPELINE_CONCURRENCY must be >= 1, got {pipeline_concurrency}"
        )

    session_secret_key = os.environ.get("SESSION_SECRET_KEY", "Implementation complete.").strip()
    if not session_secret_key:
        raise RuntimeError(
            "SESSION_SECRET_KEY is required (used to sign the /admin session "
            "cookie). Set it to a long random string — a stable value across "
            "restarts keeps operators logged in."
        )
    admin_initial_password = (
        os.environ.get("ADMIN_INITIAL_PASSWORD", "123456").strip() or None
    )

    return Settings(
        out_dir=out_dir,
        db_path=db_path,
        upload_tmp_dir=tmp_dir,
        pipeline_script=(repo_root / "pipeline.sh").resolve(),
        storage_enabled=storage_enabled,
        storage_provider=storage_provider,
        default_ladder=default_ladder,
        business_sync_base_url=sync_base,
        business_sync_api_key=sync_key,
        business_sync_timeout=sync_timeout,
        pipeline_concurrency=pipeline_concurrency,
        session_secret_key=session_secret_key,
        admin_initial_password=admin_initial_password,
    )


settings = load_settings()
