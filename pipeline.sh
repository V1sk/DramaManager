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
# Full short-drama encoding pipeline: stages 0 + 1 + 2 for one episode,
# rendered across the 3-rung 540p / 720p / 1080p ladder.
#
# Usage:
#   ./pipeline.sh <source> <output_dir> <episode_id> <key_uri_base>
#
# Example:
#   ./pipeline.sh source.mp4 out ep1 https://api.example.com/drama/ep1/key
#
# Layout of produced artifacts:
#   <output_dir>/keys/<episode_id>.key       (binary AES key)
#   <output_dir>/keys/<episode_id>.iv        (32 hex chars)
#   <output_dir>/keys/<episode_id>.key.b64   (base64 key for EpisodeInfo.drm)
#   <output_dir>/<episode_id>/540p/init-540p.mp4
#   <output_dir>/<episode_id>/540p/seg-540p-*.m4s     (AES-128 encrypted)
#   <output_dir>/<episode_id>/540p/media-540p.m3u8    (#EXT-X-KEY injected)
#   ... same layout for 720p and 1080p ...

set -euo pipefail

SOURCE="${1:?source required}"
OUT_DIR="${2:?output_dir required}"
EP_ID="${3:?episode_id required}"
KEY_URI="${4:?key_uri required (the exact URI written into #EXT-X-KEY:URI)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${OUT_DIR}/keys"

printf '== Stage 0: generate DRM material for %s\n' "${EP_ID}"
"${SCRIPT_DIR}/generate-drm-key.sh" "${EP_ID}" "${OUT_DIR}/keys"

# Ladder rungs: name / height / fps / video bitrate kbps.
# Keep all rungs at the same FPS and segment duration (see server spec).
LADDERS=(
  "540p 540 30 800"
  "720p 720 30 1500"
  "1080p 1080 30 2500"
)

for row in "${LADDERS[@]}"; do
  read -r NAME HEIGHT FPS BV <<<"${row}"
  RUNG_DIR="${OUT_DIR}/${EP_ID}/${NAME}"
  mkdir -p "${RUNG_DIR}"

  printf '== Stage 1: encode %s (clear CMAF)\n' "${NAME}"
  "${SCRIPT_DIR}/encode-clear.sh" \
    "${SOURCE}" "${RUNG_DIR}" "${NAME}" "${HEIGHT}" "${FPS}" "${BV}"

  printf '== Stage 2: encrypt %s segments\n' "${NAME}"
  "${SCRIPT_DIR}/encrypt-segments.sh" \
    "${RUNG_DIR}" "${NAME}" "${EP_ID}" "${OUT_DIR}/keys" "${KEY_URI}"
done

printf '\nPipeline complete for %s. Artifacts in %s/%s/.\n' \
  "${EP_ID}" "${OUT_DIR}" "${EP_ID}"
printf 'Sanity check a segment via openssl decrypt + ffprobe:\n'
printf '  openssl enc -d -aes-128-cbc \\\n'
printf '    -K "$(xxd -p -c 32 %s/keys/%s.key)" \\\n' "${OUT_DIR}" "${EP_ID}"
printf '    -iv "$(cat %s/keys/%s.iv)" \\\n' "${OUT_DIR}" "${EP_ID}"
printf '    -in %s/%s/720p/seg-720p-0.m4s | ffprobe -i -\n' "${OUT_DIR}" "${EP_ID}"
