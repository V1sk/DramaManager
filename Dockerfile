# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Pipeline native deps (NOT pip-installable):
#   ffmpeg / ffprobe — encoding + probing source video
#   openssl          — AES-128-CBC segment encryption in encrypt-segments.sh
#   xxd              — key file (hex/base64) conversions in pipeline.sh
#   bash, awk        — pipeline.sh / encode-clear.sh / encrypt-segments.sh
# bash + awk ship with debian-slim; the other three are explicit installs.
# ffmpeg version comes from the Debian repo (whatever the base image ships).
# Local dev runs on ffmpeg 8.x; the pipeline is not bound to a specific
# version — the `-hls_key_info_file` workaround documented in CLAUDE.md is
# an upstream bug class, not version-specific.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      openssl \
      xxd \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps before COPY-ing app code so the requirements layer
# caches across most edits — `pip install` is the slowest step that doesn't
# need to rerun for code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore excludes hls.db* / out / tmp / venv / credentials.py / __pycache__
# so the image stays small and doesn't bake host state or secrets.
COPY . .

# Pipeline scripts must be executable in the image layer (chmod on host bind
# mounts would be a fragile contract).
RUN chmod +x pipeline.sh generate-drm-key.sh encode-clear.sh encrypt-segments.sh

# Persistent paths live under /data, bind-mounted by compose. Defaults match
# the compose layout so `docker run` without compose still works for ad-hoc
# debugging. Override via -e or env_file when needed.
ENV OUT_DIR=/data/out \
    DB_PATH=/data/hls.db \
    UPLOAD_TMP_DIR=/data/tmp

EXPOSE 8000

# Single uvicorn process by design: the encode queue + per-episode locks
# live in-process (asyncio.Queue + asyncio.Lock). PIPELINE_CONCURRENCY is
# the right knob for parallel encoding; uvicorn --workers > 1 would split
# the queue across processes and break those invariants.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
