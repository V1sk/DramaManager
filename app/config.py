import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    out_dir: Path
    db_path: Path
    upload_tmp_dir: Path
    pipeline_script: Path
    oss_enabled: bool


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

    return Settings(
        out_dir=out_dir,
        db_path=db_path,
        upload_tmp_dir=tmp_dir,
        pipeline_script=(repo_root / "pipeline.sh").resolve(),
        oss_enabled=_parse_bool_env("OSS_ENABLED"),
    )


settings = load_settings()
