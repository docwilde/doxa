# Opt-in Mermaid CLI validation

This harness tests the production bubblewrap command with a pinned real
Mermaid CLI and four diagram families. It is separate from the DOXA installer.
It does not download a browser or install Node. Only explicitly provision a
renderer after reviewing its package and dedicated root outside DOXA state and
session repositories.

The lockfile pins `@mermaid-js/mermaid-cli` **12.0.0** and Puppeteer **25.13.0**,
with the KaTeX **0.18.2** override. The fixture pins Chrome for Testing
**131.0.6778.85**. The checked-in wrapper points Puppeteer at
`/renderer/chrome/chrome`, the path visible inside DOXA's sandbox. Its
`--no-sandbox` browser flag relies on the enclosing networkless bubblewrap
sandbox; do not run the wrapper outside that sandbox.

## Optional offline setup

Review `renderer.sh`, `puppeteer-config.json`, `package.json`, the lockfile and
`prepare.py`. The helper requires Linux x86-64, installed `/usr/bin/node` and
`/usr/bin/npm`, an existing populated npm cache, and the existing pinned Chrome
directory. It never fetches packages, runs lifecycle scripts, downloads a
browser or changes DOXA settings. Allow about **850 MiB** for the dedicated
package and browser. First print the plan:

```bash
root="$HOME/.local/share/doxa-mermaid/12.0.0"
browser="$HOME/.cache/puppeteer/chrome/linux-131.0.6778.85/chrome-linux64"
cache="$HOME/.npm" # must already contain every locked package archive
python3 scripts/mermaid-validation/prepare.py \
  --root "$root" --browser "$browser" --npm-cache "$cache"
```

Repeat that command with `--provision` to perform the offline install. It
refuses an existing destination, browser symlinks, repository roots and DOXA
state directories. `npm ci --offline --ignore-scripts` checks locked archive
integrity; missing cached archives fail instead of enabling network access.
A failed attempt leaves its dedicated directory for inspection. No provisioning
step runs during DOXA build, release or normal installation.

After provisioning, use a private scratch directory outside `/tmp` and run:

```bash
export DOXA_MERMAID_VALIDATION_ROOT="$root"
export TMPDIR="$HOME/.cache/doxa-mermaid-scratch"
install -d -m 700 "$TMPDIR"
scripts/mermaid-validation/validate.sh
```

The script verifies the checked-in wrapper, config, manifest and lock, exact
package and browser versions, then runs the existing sandbox doctor and
flowchart, sequence, class and Gantt fixtures. Each fixture must yield a bounded
PNG decodable into the portable halfblock protocol. It prints only fixture
names and static failure reasons, never transcript text or renderer output.

Set **Settings → Appearance → Mermaid renderer executable** to
`$root/renderer.sh` and **Mermaid renderer package root** to `$root` (use
expanded absolute paths). Press **Ctrl+P** on either row to preflight before
explicitly saving. Keep `image mode=halfblock` for a standard Mint/VTE terminal;
the four-row overview supplements the readable source below it.

## Native terminal trial

The acceptance fixture uses the production App with synthetic transcript data:

```bash
DOXA_MERMAID_RENDERER="$root/renderer.sh" \
DOXA_MERMAID_RENDERER_ROOT="$root" DOXA_IMAGE_MODE=halfblock \
  cargo run --locked -p doxa-tui --example mermaid-terminal -- flowchart --terminal
```

Repeat with `sequence`, `class`, or `gantt`. Ctrl+C closes the fixture.
`DOXA_IMAGE_MODE=text` checks source-only behavior. These commands do not start
a provider session or use the clipboard. `TMPDIR` must remain private.

## Host validation, 2026-10-09

Linux `7.0.0-34-generic` x86-64 has Node `22.22.1`, npm `9.2.0`, bubblewrap
`0.11.1`, Google Chrome `154.0.8037.97`, Chromium snap `155.0.8059.39`, and
a cached Chrome for Testing `131.0.6778.85`. In an isolated task-local root,
`npm ci` installed the exact lockfile tree with browser downloads and lifecycle
scripts disabled. The lockfile contains 205 entries, and each resolved archive
has an integrity hash; `npm ls --all` confirmed Mermaid CLI `12.0.0` and
Puppeteer `25.13.0`. The copied browser binary matched the cached one
byte-for-byte and reported the pinned version. A configured
`doxa doctor --engine fixture` smoke render passed, followed by bounded
flowchart, sequence, class and Gantt sandbox render and halfblock decode tests.
The agent terminal reported `TERM=dumb`; on-screen quality was not exercised.

## Host validation, 2026-10-20

The explicit offline helper rebuilt the exact locked package from a retained
review cache, with scripts and browser downloads disabled. The dedicated
package occupies 822 MiB (484 MiB packages plus 338 MiB browser). The copied
browser SHA-256 matches its cached source. The installed beta.63 doctor and the
pinned four-family sandbox suite passed again.

Actual production-App rendering was captured in GTK3/VTE **0.84.0**, using
DejaVu Sans Mono and a private Xvfb display at **125 columns × 33 rows**. All
four diagram families produced terminal halfblock cells. That trial exposed
the four-row limit: eight vertical color samples cannot preserve small diagram
labels. DOXA now retains the original, copyable source beneath each overview.
This is a portable overview, not a full-size diagram viewer.

Kitty, Sixel and iTerm2 display quality remains untested: none of those real
emulators is available on this host. macOS rendering remains disabled because
the production isolation boundary requires Linux bubblewrap; macOS needs a
separately reviewed implementation. No protocol or macOS result is inferred
from PNG decoding or from the VTE trial. No Mermaid CLI was installed globally.
