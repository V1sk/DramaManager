import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
    # ai-translate: optional integration with a kie.ai-style OpenAI-compatible
    # chat-completions endpoint, used to auto-translate drama name / synopsis,
    # tag labels, and actor names into every registered language. The feature
    # is enabled iff `ai_translate_api_key` is set (see `ai_translate_enabled`).
    # `ai_translate_base_url` always has a sane default; `ai_translate_model`
    # selects the kie.ai market path segment (`/<model>/v1/chat/completions`).
    ai_translate_base_url: str
    ai_translate_api_key: str | None
    ai_translate_model: str
    ai_translate_timeout: int
    # ai-translation-queue: how many translation jobs run concurrently in the
    # background worker pool. Kept small by default to bound kie.ai rate/cost;
    # raise only if the provider quota comfortably allows it. Integer >= 1.
    ai_translate_concurrency: int

    @property
    def ai_translate_enabled(self) -> bool:
        """True iff AI short-text translation is configured (API key present)."""
        return bool(self.ai_translate_api_key)


def _parse_bool_env(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"true", "1", "yes"}


def load_settings() -> Settings:
    repo_root = Path(__file__).resolve().parent.parent
    # Load .env from the repo root so a direct `uvicorn app.main:app` run (no
    # docker-compose env_file) still picks up secrets like AI_TRANSLATE_API_KEY.
    # override=False → real environment vars and docker-compose's env_file keep
    # precedence; .env only fills what isn't already set.
    load_dotenv(repo_root / ".env", override=False)
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

    sync_base = (os.environ.get("BUSINESS_SYNC_BASE_URL", "https://agent.coocent.com/drama") or "").strip() or None
    sync_key = (os.environ.get("BUSINESS_SYNC_API_KEY", "Coocent@Video") or "").strip() or None
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

    session_secret_key = os.environ.get("SESSION_SECRET_KEY", "636d6260e6dc58c9e8dec39a03df97da4e8c49e3de0f79521b82a4bdb0d4db20").strip()
    if not session_secret_key:
        session_secret_key = secrets.token_hex(32)
        # logging isn't configured yet at this point (basicConfig runs in
        # app/main.py); write directly to stderr so the warning still surfaces.
        print(
            "WARNING [admin-accounts-auth]: SESSION_SECRET_KEY 未设置 → "
            "已在内存里随机生成一个 key，进程重启后所有已登录会话失效。"
            "需要重启间保持登录的部署请显式设一个长随机串 "
            "(openssl rand -hex 32) 写到 env。",
            file=sys.stderr,
            flush=True,
        )
    admin_initial_password = (
        os.environ.get("ADMIN_INITIAL_PASSWORD", "123456").strip() or None
    )

    # ai-translate: optional kie.ai-style chat-completions endpoint. The API key
    # is the on/off gate (feature disabled when unset → routes 503, UI buttons
    # hidden). base_url / model have sane defaults; timeout is generous because
    # a single call may translate several fields into many languages at once.
    ai_base = (os.environ.get("AI_TRANSLATE_BASE_URL", "https://api.kie.ai") or "").strip()
    if not ai_base:
        ai_base = "https://api.kie.ai"
    if ai_base.endswith("/"):
        ai_base = ai_base.rstrip("/")
    ai_key = (os.environ.get("AI_TRANSLATE_API_KEY", "") or "").strip() or None
    ai_model = (os.environ.get("AI_TRANSLATE_MODEL", "gpt-5-2") or "").strip() or "gpt-5-2"
    ai_timeout_raw = os.environ.get("AI_TRANSLATE_TIMEOUT", "120").strip()
    try:
        ai_timeout = int(ai_timeout_raw) if ai_timeout_raw else 120
    except ValueError as e:
        raise RuntimeError(
            f"AI_TRANSLATE_TIMEOUT must be an integer (seconds), got {ai_timeout_raw!r}"
        ) from e
    if ai_timeout <= 0:
        raise RuntimeError(
            f"AI_TRANSLATE_TIMEOUT must be positive, got {ai_timeout}"
        )
    ai_concurrency_raw = os.environ.get("AI_TRANSLATE_CONCURRENCY", "2").strip()
    try:
        ai_concurrency = int(ai_concurrency_raw) if ai_concurrency_raw else 2
    except ValueError as e:
        raise RuntimeError(
            f"AI_TRANSLATE_CONCURRENCY must be an integer, got {ai_concurrency_raw!r}"
        ) from e
    if ai_concurrency < 1:
        raise RuntimeError(
            f"AI_TRANSLATE_CONCURRENCY must be >= 1, got {ai_concurrency}"
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
        ai_translate_base_url=ai_base,
        ai_translate_api_key=ai_key,
        ai_translate_model=ai_model,
        ai_translate_timeout=ai_timeout,
        ai_translate_concurrency=ai_concurrency,
    )


settings = load_settings()
