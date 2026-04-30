#!/usr/bin/env bash
# Copyright 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Stage 0 of the short-drama encryption pipeline: generate per-episode DRM
# material. Each episode gets its own 16-byte AES-128 key and 16-byte IV.
#
# Usage:
#   ./generate-drm-key.sh <episode_id> <output_dir>
#
# Produces:
#   <output_dir>/<episode_id>.key      — raw 16-byte AES key (server-held only)
#   <output_dir>/<episode_id>.iv       — 32 hex chars, write to #EXT-X-KEY:IV
#   <output_dir>/<episode_id>.key.b64  — base64 key for EpisodeInfo.drm.keyBase64

set -euo pipefail

EP_ID="${1:?usage: generate-drm-key.sh <episode_id> <output_dir>}"
OUT_DIR="${2:?usage: generate-drm-key.sh <episode_id> <output_dir>}"

mkdir -p "${OUT_DIR}"

KEY_PATH="${OUT_DIR}/${EP_ID}.key"
IV_PATH="${OUT_DIR}/${EP_ID}.iv"
KEY_B64_PATH="${OUT_DIR}/${EP_ID}.key.b64"

openssl rand -out "${KEY_PATH}" 16
openssl rand -hex 16 > "${IV_PATH}"
base64 -i "${KEY_PATH}" > "${KEY_B64_PATH}"

printf 'Generated DRM material for %s:\n' "${EP_ID}"
printf '  %s (binary 16 bytes, keep on server)\n' "${KEY_PATH}"
printf '  %s (32 hex chars, goes into #EXT-X-KEY:IV)\n' "${IV_PATH}"
printf '  %s (base64, ship in EpisodeInfo.drm.keyBase64)\n' "${KEY_B64_PATH}"
