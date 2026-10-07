#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo 'usage: scripts/package-browser-extension.sh /absolute/output.zip' >&2
  exit 2
fi
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output="$1"
mkdir -p "$(dirname "$output")"
if [[ -e "$output" ]]; then
  echo "output already exists: $output" >&2
  exit 1
fi
cd "$repo_root/browser-extension"
zip -q -X "$output" manifest.json background.js app.html app.css app.mjs client.mjs crypto.mjs icon.png
unzip -Z -1 "$output"
