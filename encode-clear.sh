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
# Stage 1 of the short-drama encryption pipeline: produce a single ladder rung
# as clear CMAF (unencrypted init.mp4 + .m4s segments + media playlist).
#
# This stage DOES NOT call FFmpeg's `-hls_key_info_file` — the spec
# (openspec/changes/short-drama-fast-startup/specs/shortdrama-server/spec.md)
# explicitly disables that path because of FFmpeg fMP4+AES bugs. Use stage 2
# (encrypt-segments.sh) to apply AES-128 after this script finishes.
#
# Usage:
#   ./encode-clear.sh <source> <output_dir> <ladder> <height> <fps> <bitrate_kbps> [maxrate_kbps] [bufsize_kbps]
#
# Example:
#   ./encode-clear.sh source.mp4 out 720p 720 30 1500

set -euo pipefail

SOURCE="${1:?source file required}"
OUT_DIR="${2:?output_dir required}"
LADDER="${3:?ladder name required (e.g. 540p / 720p / 1080p)}"
HEIGHT="${4:?height in pixels required}"
FPS="${5:?target fps required}"
BV="${6:?video bitrate (kbps) required}"
MAXRATE="${7:-$((BV * 120 / 100))}"   # default 1.2x BV
BUFSIZE="${8:-$((BV * 2))}"           # default 2x BV

# Resolve SOURCE to an absolute path BEFORE cd — otherwise a relative input
# like `source.mp4` becomes unreachable once we move into the output dir.
if [[ ! -f "${SOURCE}" ]]; then
  echo "source file not found: ${SOURCE}" >&2
  exit 1
fi
SOURCE="$(cd "$(dirname "${SOURCE}")" && pwd)/$(basename "${SOURCE}")"

mkdir -p "${OUT_DIR}"
cd "${OUT_DIR}"

ffmpeg -y -i "${SOURCE}" \
  -c:v libx264 -preset slow \
  -b:v "${BV}k" -maxrate "${MAXRATE}k" -bufsize "${BUFSIZE}k" \
  -vf "scale=-2:${HEIGHT}" \
  -r "${FPS}" \
  -g "$((FPS * 2))" -keyint_min "$((FPS * 2))" -sc_threshold 0 \
  -force_key_frames "expr:gte(t,n_forced*2)" \
  -c:a aac -ar 44100 -b:a 128k -ac 2 \
  -hls_time 2 \
  -hls_segment_type fmp4 \
  -hls_fmp4_init_filename "init-${LADDER}.mp4" \
  -hls_segment_filename "seg-${LADDER}-%d.m4s" \
  -hls_playlist_type vod \
  -hls_flags independent_segments+program_date_time \
  -hls_list_size 0 \
  -master_pl_name "" \
  "media-${LADDER}.m3u8"

printf 'Clear CMAF produced for ladder %s in %s:\n' "${LADDER}" "${OUT_DIR}"
printf '  init-%s.mp4  (unencrypted — stays clear after stage 2)\n' "${LADDER}"
printf '  seg-%s-*.m4s (plain bytes — stage 2 encrypts these in place)\n' "${LADDER}"
printf '  media-%s.m3u8 (no #EXT-X-KEY yet — stage 2 injects it)\n' "${LADDER}"
