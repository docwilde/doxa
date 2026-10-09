#!/bin/sh
# Copy into the owner-reviewed package root. DOXA mounts that root at /renderer.
set -eu
exec /usr/bin/node /renderer/node_modules/@mermaid-js/mermaid-cli/src/cli.js \
  -p /renderer/puppeteer-config.json "$@"
