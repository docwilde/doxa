#!/usr/bin/env bash
# Explicit local validation only. Never provisions Node, Chromium, or a renderer.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../.." && pwd)
fail() { printf 'Mermaid validation unavailable: %s\n' "$1" >&2; exit 1; }
: "${DOXA_MERMAID_VALIDATION_ROOT:?set the owner-reviewed renderer package root}"
: "${TMPDIR:?set a private scratch directory outside /tmp}"
case "$TMPDIR" in /tmp|/tmp/*) fail 'choose a private scratch directory outside /tmp';; esac
root=$(realpath -e -- "$DOXA_MERMAID_VALIDATION_ROOT" 2>/dev/null) || fail 'package root is unavailable'
renderer="$root/renderer.sh"
chrome="$root/chrome/chrome"
package="$root/node_modules/@mermaid-js/mermaid-cli/package.json"
puppeteer_package="$root/node_modules/puppeteer/package.json"

[[ -f "$package" && -f "$puppeteer_package" && -x "$renderer" && -x "$chrome" ]] \
  || fail 'the pinned package, wrapper, or browser is missing'
cmp -s -- "$script_dir/renderer.sh" "$renderer" || fail 'renderer wrapper differs from the reviewed fixture'
cmp -s -- "$script_dir/puppeteer-config.json" "$root/puppeteer-config.json" || fail 'browser config differs from the reviewed fixture'
resolved_chrome=$(realpath -e -- "$chrome" 2>/dev/null) || fail 'browser path is unavailable'
case "$resolved_chrome" in "$root"/*) ;; *) fail 'browser escapes the package root';; esac
node -e 'const p=require(process.argv[1]); if(p.version!=="12.0.0") process.exit(1)' "$package" 2>/dev/null \
  || fail 'Mermaid CLI is not the pinned 12.0.0 release'
node -e 'const p=require(process.argv[1]); if(p.version!=="25.13.0") process.exit(1)' "$puppeteer_package" 2>/dev/null \
  || fail 'Puppeteer is not the pinned 25.13.0 release'
browser_version=$("$chrome" --version 2>/dev/null) || fail 'browser version probe failed'
[[ "$browser_version" =~ ^Google\ Chrome\ for\ Testing\ 131\.0\.6778\.85[[:space:]]*$ ]] \
  || fail 'browser is not the pinned Chrome for Testing 131.0.6778.85'

printf 'Mermaid CLI: 12.0.0\nPuppeteer: 25.13.0 (lockfile)\nBrowser: %s\nNode: %s\n' \
  "$browser_version" "$(node --version)"
printf 'Bubblewrap: %s\nHost: %s\nTerminal: TERM=%s; graphics protocol not exercised\n' \
  "$(/usr/bin/bwrap --version)" "$(uname -srm)" "${TERM:-unset}"
cd -- "$repo_dir"
DOXA_MERMAID_VALIDATION_RENDERER="$renderer" \
DOXA_MERMAID_VALIDATION_PACKAGE_ROOT="$root" \
  cargo test --locked -p doxa-tui --lib real_cli_fixture_suite -- --ignored --nocapture
