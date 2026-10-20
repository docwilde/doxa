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
`DOXA_IMAGE_MODE=text` checks source-only behavior. On a supported graphics
terminal, repeat with `DOXA_IMAGE_MODE=probe`, or its actual supported protocol:
`kgp` for Kitty, `sixel` for Sixel-enabled Xterm, or `iterm2` for WezTerm. Forced
graphics uses reported terminal pixel dimensions; when they are unknown it
shows a notice and falls back to halfblocks. These commands do not start
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

An additional actual Kitty **0.45.0** trial used authenticated Ubuntu
`0.45.0-1build1` packages, downloaded with apt and SHA-256 checked against its
package index. Kitty, shell integration and terminfo totalled **9,901,364 bytes**
of downloads and **34 MiB** privately extracted. All native/Python dependencies
were already installed; no package was installed system-wide. Private Xvfb
and software Mesa **26.0.8 / OpenGL 4.5** supplied the actual emulator display.

The trial exposed two production defects: successful `probe` negotiation was
subsequently overridden with halfblocks, and forced graphics used fixed 8×16
cell dimensions. Both were fixed. A transparent private PTY recording captured
real Kitty `OK`, device attributes, 16×32 cell-size and status responses with
timestamps. Direct production-App trials then rendered all four families in
both `kgp` and `probe`: **eight final native captures**, with real KGP image
transmissions and zero halfblock cells. The four-row flowchart changed from
224×64 to 448×128 pixels; visual inspection confirmed Start/Ready?/Done labels.
Sequence and class labels remain small within four rows, so source retention
is still needed. Owner image-mode settings and the Mint/VTE fallback did not
change.

The beta.64 fixture was then built from `d9a4c12d` in release mode and copied
to private task storage. It exercised two more actual emulators under private
Xvfb; no installed DOXA binary or owner image-mode setting changed:

| Actual Linux emulator | Authenticated private package | Modes | Four-row preview |
| --- | --- | --- | --- |
| [Xterm 407](https://invisible-island.net/xterm/manpage/xterm.html), Sixel enabled with VT340 emulation | Ubuntu `407-1ubuntu1` plus xbitmaps; 949,294 bytes downloaded | `sixel`, `probe` | Reported 17×33 pixel cells; 132 pixels high |
| [WezTerm 20240203-110809-5046fc22](https://wezterm.org/imgcat.html), iTerm2 inline-image protocol | [Official signed apt repository](https://wezterm.org/install/linux.html); 34,300,188 bytes downloaded | `iterm2`, `probe` | Reported 16×32 pixel cells; 128 pixels high |

Packages were SHA-256 checked against authenticated apt indexes and extracted
only into task storage; no maintainer scripts or system installation ran. All
native libraries were already available. Both terminals used DejaVu Sans Mono,
125 columns × 33 rows, on a 2304×1344 private display.

All four families passed in each mode: **16 final native captures**. Transparent
PTY recording retained timestamped real device replies and actual Sixel DCS or
iTerm2 OSC 1337 PNG payloads, with zero halfblock cells. For every family,
forced/probe protocol payloads and the transcript screenshot region were
identical. All eight forced-mode images were visually inspected: diagrams
rendered without obscuring their source; simple flowchart labels were readable,
while dense sequence/class labels remained small. No production change was
needed. These trials did not include tmux or SSH transport.

The private acceptance archive records the source/binary hashes, signed-package
receipts, capture hashes, raw replies and image comparisons. It contains only
synthetic fixtures, with no provider or owner session data. The initial capture
harness was corrected to wait for stable image payloads rather than stable raw
byte counts, and the final display includes the entire terminal window.

[WezTerm documents inline-image support](https://wezterm.org/imgcat.html) for
the [iTerm2 protocol](https://iterm2.com/documentation-images.html). This is
Linux protocol display evidence, not actual iTerm2 app acceptance.
No Mac was available. macOS rendering remains disabled because the production
isolation boundary requires Linux bubblewrap. The
[macOS design](../../docs/plans/mermaid-macos-isolation.md) now scopes a dedicated
networkless VM and explicit host acceptance gates; implementation, actual Mac
containment, iTerm2 app and Retina validation remain open. No Mermaid CLI was
installed globally.
