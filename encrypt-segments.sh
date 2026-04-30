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
# Stage 2 of the short-drama encryption pipeline: AES-128-CBC + PKCS7 encrypt
# every .m4s segment in place and inject #EXT-X-KEY into the playlist.
#
# Important:
#   - init.mp4 is NEVER encrypted. Its inclusion BEFORE #EXT-X-KEY in the m3u8
#     keeps it outside the key's scope — matching ExoPlayer's Aes128DataSource
#     expectations.
#   - `openssl enc` defaults to PKCS7 padding; do NOT pass `-nopad`.
#   - The key hex and IV hex are read from the files produced by
#     generate-drm-key.sh (stage 0).
#
# Usage:
#   ./encrypt-segments.sh <output_dir> <ladder> <episode_id> <key_dir> <key_uri>
#
# Example:
#   ./encrypt-segments.sh out/ep1/720p 720p ep1 out/keys \
#       https://api.example.com/drama/ep1/key

set -euo pipefail

OUT_DIR="${1:?output_dir required}"
LADDER="${2:?ladder required}"
EP_ID="${3:?episode_id required}"
KEY_DIR="${4:?key_dir required}"
KEY_URI="${5:?key_uri required (exact string written to #EXT-X-KEY:URI)}"

KEY_FILE="${KEY_DIR}/${EP_ID}.key"
IV_FILE="${KEY_DIR}/${EP_ID}.iv"

[[ -f "${KEY_FILE}" ]] || { echo "missing key file ${KEY_FILE}"; exit 1; }
[[ -f "${IV_FILE}" ]]  || { echo "missing iv file ${IV_FILE}";  exit 1; }

KEY_HEX=$(xxd -p -c 32 "${KEY_FILE}")
IV_HEX=$(tr -d '\n' < "${IV_FILE}")

cd "${OUT_DIR}"

shopt -s nullglob
segments=( "seg-${LADDER}-"*.m4s )
shopt -u nullglob

if (( ${#segments[@]} == 0 )); then
  echo "no segments matched seg-${LADDER}-*.m4s in ${OUT_DIR}" >&2
  exit 1
fi

for seg in "${segments[@]}"; do
  openssl enc -aes-128-cbc -K "${KEY_HEX}" -iv "${IV_HEX}" \
    -in "${seg}" -out "${seg}.enc"
  mv "${seg}.enc" "${seg}"
done

PL="media-${LADDER}.m3u8"
[[ -f "${PL}" ]] || { echo "missing playlist ${PL}"; exit 1; }

awk -v key_uri="${KEY_URI}" -v iv_hex="${IV_HEX}" '
  BEGIN { inserted = 0 }
  /^#EXT-X-MAP:/ { print; next }
  /^#EXTINF:/ && !inserted {
    print "#EXT-X-KEY:METHOD=AES-128,URI=\"" key_uri "\",IV=0x" iv_hex
    inserted = 1
  }
  { print }
' "${PL}" > "${PL}.tmp"
mv "${PL}.tmp" "${PL}"

printf 'Encrypted %d segments in %s and injected #EXT-X-KEY into %s\n' \
  "${#segments[@]}" "${OUT_DIR}" "${PL}"
