# Opt-in Mermaid CLI validation

This harness tests the production bubblewrap command with a pinned real
Mermaid CLI and four diagram families. It is separate from the DOXA installer.
It neither installs a renderer nor downloads a browser. Run it only after
reviewing the package and putting it in a dedicated private directory outside
DOXA state and session repositories.

The lockfile pins `@mermaid-js/mermaid-cli` **12.0.0** and Puppeteer **25.13.0**.
The fixture pins Chrome for Testing **131.0.6778.85**. The checked-in wrapper
points Puppeteer at `/renderer/chrome/chrome`, the path visible inside DOXA's
sandbox. Its `--no-sandbox` browser flag relies on the enclosing networkless
bubblewrap sandbox; do not run the wrapper outside that sandbox.

For an explicit local trial on this host, review these files, then provision
the package yourself:

```bash
root="$HOME/opt/doxa-mermaid-validation"
install -d -m 700 "$root" "$root/chrome"
cp scripts/mermaid-validation/package{,-lock}.json "$root/"
PUPPETEER_SKIP_DOWNLOAD=1 npm ci --prefix "$root" --ignore-scripts --omit=dev
cp scripts/mermaid-validation/{renderer.sh,puppeteer-config.json} "$root/"
chmod 700 "$root/renderer.sh"
cp -a "$HOME/.cache/puppeteer/chrome/linux-131.0.6778.85/chrome-linux64/." "$root/chrome/"
export DOXA_MERMAID_VALIDATION_ROOT="$root"
export TMPDIR=/home/docwilde/t
scripts/mermaid-validation/validate.sh
```

The copy is about 338 MiB on the measured host. The script verifies the
checked-in wrapper and config, exact package and browser versions, then runs
the existing `diagnose` sandbox doctor and flowchart, sequence, class and
Gantt fixtures. Each fixture must yield a bounded PNG decodable into the
portable halfblock protocol. It prints only fixture names and static failure
reasons, never transcript text or renderer output. A successful run still
needs a real terminal trial for Kitty, Sixel and iTerm2 quality.

## Host observation, 2026-10-09

Linux `7.0.0-34-generic` x86-64 has Node `22.22.1`, npm `9.2.0`, bubblewrap
`0.11.1`, Google Chrome `154.0.8037.97`, Chromium snap `155.0.8059.39`, and
a cached Chrome for Testing `131.0.6778.85`. The DOXA-style bubblewrap
capability probe succeeded with the system runtime mounts. No Mermaid CLI is
installed globally or on `PATH`; the locked package and browser have not been
copied to an owner-reviewed root. The four real CLI fixtures and `doxa doctor`
against that package therefore remain unverified. The agent terminal reports
`TERM=dumb`, so no Kitty, Sixel, iTerm2 or on-screen halfblock quality claim is
made. The regular stub-renderer tests validate fallback and bounded decoding,
not CLI syntax fidelity.

The optional `npm ci` command above fetches locked JavaScript packages; its
explicit download skip and `--ignore-scripts` prevent Puppeteer from installing
a browser. It does not install Node. No provisioning command runs as part of
the DOXA build, release or default installer.
