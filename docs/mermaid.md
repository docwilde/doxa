# Mermaid diagrams in the transcript — specification

Status: **draft for review**. Nothing implemented.

## What happens today

A ```mermaid fence renders as a fenced code block — Textual's markdown gives it
a `MarkdownFence` widget and the user reads the source. That is not a failure:
the source is legible, and it is what every other terminal client does.

## What already exists to build on

v0.41.0 built the whole image path, and this feature is mostly a new *source*
for it rather than new plumbing:

- `doxa/images.py` — a probed ladder (`kgp` → `sixel` → `halfblock` → `text`),
  `widget_for(source, desc, mode=None)` that ALWAYS returns a mountable widget
  and never raises, and `DOXA_IMAGE_MODE` to force a tier.
- PIL is a runtime dependency, and v0.41.0 already composites RGBA onto the
  theme background rather than letting `convert("RGB")` discard alpha (which
  produced a white slab the first time).
- `/img` reports what the terminal actually answered, labels rungs the ladder
  short-circuited past as never asked, and distinguishes a measured cell size
  from a defaulted one.

So: given a PNG, DOXA can already draw it well and degrade honestly. The whole
question is how a mermaid *string* becomes that PNG.

## The renderer problem, and why it is the whole spec

**Mermaid is JavaScript.** There is no complete Python implementation. The
options are genuinely different products, not variations:

| renderer | fidelity | what it costs | verdict |
|---|---|---|---|
| `mmdc` (mermaid-cli) | full — every diagram type, official | Node **plus a headless Chromium** (~400 MB via puppeteer) | the only complete option |
| graphviz (`dot`) via a mermaid→dot translation | flowcharts and simple graphs only; sequence/gantt/class do not map | a small C binary, no browser | a real fallback, not a substitute |
| `mermaid.ink` / any hosted renderer | full | **sends the diagram to a third party** | rejected — see below |
| leave the fence | none | nothing | today's behaviour, and the floor |

**The hosted option is rejected by default and should stay that way.** DOXA's
transcripts are private by construction — LORE scrubs secrets before anything
reaches disk, indexing never leaves the machine, and `doxa/peers.py` treats
even a peer session as untrusted. Posting a diagram of a private repository's
architecture to a public API contradicts all of it. If it is ever offered it
must be off by default, named plainly as a network call, and never the tier an
absent local renderer silently falls through to.

## The dependency is not a Python package — this is the crux

`mmdc` installs through **npm**, so it cannot be a pip extra. `doxa[mermaid]`
is not available as a mechanism. That makes this an *installer* concern rather
than a packaging one, which is exactly where the user placed it:

> let the user decide at install time if they want mermaid rendered in TUI,
> then the dependency comes with it

So `scripts/install.sh` gains a prompt, and the answer is recorded — not
re-probed on every launch, and not inferred from whether `mmdc` happens to be
on PATH. Requirements:

- **The prompt states the real cost before the answer**, in numbers: Node must
  already be present, and mermaid-cli pulls a headless Chromium of roughly
  400 MB. A user who says yes should not be surprised by what lands.
- **Declining is not a degraded install.** Fences still render as fences, which
  is what the user has today; nothing about DOXA becomes worse for saying no.
- **The installer never installs Node.** If Node is absent, say so, skip, and
  leave a one-line instruction for enabling it later. An installer that pulls a
  language runtime nobody asked for has overstepped.
- **A setting mirrors the answer** (`mermaid` / `DOXA_MERMAID`, following the
  `Setting` shape in `doxa/config.py`), so it is visible and changeable in the
  settings modal rather than frozen at install time.
- **`doxa doctor` reports it** the way v0.39.0 reports the keyboard protocol:
  which renderer is present, which tier a fence would take, and — if the
  setting is on but `mmdc` is missing — that this is why diagrams are showing
  as source.

## Rendering path

1. A ```mermaid fence is recognised at render time. Everything else stays a
   normal fence — no other language is intercepted.
2. If the setting is off, or no renderer is available: **the fence renders
   exactly as it does today.** This is the floor and it must be reachable from
   every failure, including a renderer that starts and then fails.
3. Otherwise the source is rendered to PNG and handed to `images.widget_for`,
   which applies the existing ladder. A terminal in `text` mode gets the fence
   back rather than `[image: diagram]` — the source IS the better fallback
   here, unlike a photograph.
4. Render off the UI thread. DOXA is a Textual app with a documented
   no-per-frame, no-timer discipline; spawning Chromium synchronously in a
   render path would freeze the transcript. Follow the worker pattern used
   throughout, and show the fence until the image is ready rather than a gap.

## Safety — a model-authored string reaching a browser

This deserves stating because it is easy to miss: the diagram source is written
by **the model**, and `mmdc` renders it in a headless browser. That is a real
execution surface, and closer to `!` shell in kind than to displaying an image.

- **A timeout with a hard kill**, following `doxa/shell.py`'s
  `start_new_session` + `killpg` shape — a wedged Chromium must not survive its
  turn or hold the session.
- **Bounded input.** Cap the fence size that is rendered at all; a pathological
  diagram is a resource attack with no upstream limit.
- **No network in the renderer.** `mmdc` supports diagrams that fetch remote
  content; that must be off, or the transcript becomes an exfiltration path for
  anything the model can encode into a URL.
- **A render failure is a fence, never a stack trace** in the transcript.
- The rendered PNG is a temporary file DOXA owns and removes; it does not land
  next to the user's code.

## Testing bar

The v0.28.0 lesson applies with force, since this is pixels: assertions must be
about what the user sees.

- with no renderer available, a mermaid fence renders **byte-identically to
  today** — this is the regression that matters most, because it is what every
  existing user experiences
- with a stub renderer, the diagram widget mounts with **non-zero height** (the
  invisible-button defect passed every structural assertion for a release)
- a renderer that hangs is killed and the fence appears — with a test that
  actually hangs, not one that returns an error
- a renderer that exits non-zero leaves the fence and no traceback
- the setting genuinely disables interception, verified by the fence widget
  still being a fence
- `doctor` names the missing renderer as the reason when the setting is on

## Open questions

1. **Is the graphviz tier worth building at all?** It covers flowcharts —
   probably most of what an agent draws — for a fraction of Chromium's weight.
   But two renderers means two visual results for one input, and a diagram that
   looks different depending on what is installed is its own confusion.
2. **Cache rendered diagrams?** A transcript re-render (restore, resize, tab
   switch) should not re-spawn Chromium. Keyed by a hash of the source, in the
   session's own temp area, cleared with the session.
3. **Does a restored transcript re-render its diagrams?** v0.32.0 rebuilds
   transcripts from disk; a diagram would have to be re-rendered or re-shown as
   a fence. The fence is the honest default for a session that has ended.
