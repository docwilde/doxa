# Images in the Rust transcript

The Rust TUI renders Markdown images with absolute local paths inside the
session's verified workspace, including images within ordinary prose:
`See ![Build graph](/home/user/project/graph.png) and [details](https://example.com).`
The alt text stays visible above each preview; the surrounding prose and links
retain their order. Images nested inside links, lists, quotes, or formatting,
as well as relative paths and remote URLs, keep text/link rendering. The
preview is local UI content; it does not send pixels to the model or fetch
them from a network service.

`image_mode` in Settings chooses `halfblock` (the default, portable Unicode
cells), `probe` (detect Kitty, Sixel or iTerm2 graphics), a forced protocol
(`kgp`, `sixel`, `iterm2`), or `text`. A forced protocol needs a terminal
that implements it. `probe` asks the terminal for capabilities after entering
the alternate screen and can pause briefly. Text mode shows the alt label.

The TUI reserves four transcript rows per preview, with at most 32 previews
and 128 reserved image rows per transcript render. Later image tokens show
their alt text through normal Markdown rendering. This keeps scrolling and pane
resizing retain stable positions. It draws pixels only when all four rows are
visible; a clipped preview shows an alt-text hint until scrolled fully into
view. Images are decoded and encoded on bounded workers from a checked
regular-file handle. Paths outside the workspace and symlink escapes are
refused. Each preview has an 8 MiB file limit, an 8 million pixel limit,
and at most two active preview jobs. A
missing, unsupported or failed image shows an unavailable label.

## Mermaid fences

Complete, standalone `mermaid` code fences can become four-row previews with
the same terminal backends. Rendering is opt-in: set **Mermaid renderer
executable** and **Mermaid renderer package root** under Settings → Appearance,
or `DOXA_MERMAID_RENDERER` and `DOXA_MERMAID_RENDERER_ROOT`. The executable must
be an absolute path within a dedicated, reviewed package directory and accept
`-i INPUT -o OUTPUT` like Mermaid CLI. DOXA does not install or search for it.
Linux `/usr/bin/bwrap` is required. Without a configured renderer, bwrap, or
graphics mode, the original fence remains readable.
DOXA refuses a package root that would mount all of the user's home, DOXA
state, the active session repository, or the default fleet state, including
roots reached through symlinks. A dedicated package directory beneath home is
allowed if it is outside DOXA state and the session repository. A custom fleet
root outside DOXA state still requires operator review before selecting a
package root.

Each diagram is limited to 16 KiB of source. A worker runs the renderer for at
most five seconds in a private directory, with network and host filesystem
access removed. It sees only its package, system runtime files, and its input
and output. A failed or timed-out render leaves the original fence in place;
no hosted renderer is called. Completed PNGs use the same 8 MiB and 8 million
pixel decoder limits as local images. At most two renders run at a time, with
eight results cached in the TUI process. The same session also keeps up to
eight PNGs or 32 MiB in a private `0700` temporary directory; each PNG is
`0600`. Its key includes the source hash, renderer/package identity and output
width. Resizing back to a prior width can reuse its PNG. Each disk reuse
checks a bounded fingerprint of the reviewed package tree, including nested
files and symlink text without following links. A package too large or slow to
scan still renders, without disk caching. Changing renderer settings or
workspace discards the cache. A corrupt or
missing cache file is removed and rendered again; the source fence remains
visible until a valid preview is ready. The directory is removed at session
end.
`image_mode=text` always shows source. This path has stub-renderer coverage;
full Mermaid CLI and a graphics terminal have not been validated here.

Run `doxa doctor` to check a configured renderer. It validates the executable
and canonical package root against the same path policy as the TUI, including
currently discovered session repositories, then probes
bubblewrap and unprivileged user namespaces, then renders a fixed diagram to a
PNG inside the sandbox with a five-second limit per process. It reports a
specific static failure reason without printing configured paths or renderer
output. With no renderer configured, Mermaid is reported as disabled and
doctor can still pass. A configured but broken renderer makes doctor fail.
Passing the smoke check proves only that this command produced a decodable
bounded PNG in the sandbox; it does not validate Mermaid CLI compatibility,
Chromium, or a terminal graphics protocol. DOXA still has no managed renderer
installer or browser provisioning.

Before saving renderer settings, select either Mermaid field in Settings →
Appearance and press **Ctrl+P**. The TUI checks the proposed values on a worker
using the same fixed sample as `doxa doctor`. Settings shows a static failure
reason without using or displaying transcript diagram source. If you edit a
renderer value after the check, run it again; the old result is marked stale.

Terminal protocol quality depends on the emulator, tmux and SSH path;
halfblock is the portable fallback. Input attachment support is described below.

## Prompt attachments

Click the **Attach** / **Files N** chip, choose **Attach files** from the
command menu, or press **Alt+A**. Enter a local path (relative paths use the
session workspace), then press Enter or click **Add**. Spaces are accepted,
including quoted paths from terminal drag/drop. Tab switches to the attached
file list; select a file and press Delete or click **Remove**. **Done** / Esc
returns to the original prompt. Adding or pasting content never submits it.

Each pane/session draft owns its attachments. Changing tabs keeps them on the
original draft; moving a tab carries them with it. Enter submits the draft and
its files together. A rejected or uncertain delivery keeps the exact content
for **Alt+Up** recovery; inspect the session before retrying an uncertain turn.
Unsent attachments are held in TUI memory and are not saved across TUI exits.

PNG, JPEG, GIF and WebP inputs are decoded on a worker, with an 8 MiB source
limit and 32 million pixel limit. DOXA strips image metadata, freezes the first
frame, fits it within 2048×2048 pixels and sends a bounded PNG or JPEG (at most
1 MiB). At most four files and 4 MiB of normalized content accompany a prompt.
UTF-8 text files up to 64 KiB are included as quoted source data. PDF, other
binary documents, remote URLs and remote-hub attachment submission are not yet
supported. A selected file must be regular; symlinks are refused.

Images reach Claude stream-json and Codex app-server as native image content,
including Docker sessions. Bounded socket chunks carry bytes without adding
host mounts or enabling network access. GLM, DeepSeek, Router and legacy Codex
exec accept text files and explicitly refuse image attachments. Provider/model
errors remain possible if the selected model does not support images. DOXA's
transcript omits image pixels; the provider keeps its own image history. Text
file contents follow the prompt's normal persistence and scrubbing. Live prompt
cards show file labels; questions over 8 KiB show an explicit excerpt. The
provider and stored transcript retain the full input.

**Ctrl+V** checks for PNG clipboard content before falling back to text on
Linux. It requires `wl-paste` (Wayland, package `wl-clipboard`) or `xclip`
(X11). Install the appropriate desktop helper if missing. File attachment works
without a clipboard helper. macOS currently uses file attachment for images;
its existing `pbpaste` text path remains available. Fixed helper locations are
`/usr/bin` and `~/.local/bin`; DOXA does not run a shell or search `PATH`. Credential editors only
request text. Clipboard content is read only after an explicit paste action.
Terminal **Ctrl+Shift+V** pastes text through bracketed paste; it does not carry
image bytes. Copy selected transcript text with **Ctrl+C**; without a selection
Ctrl+C keeps its cancel behavior. Desktop copy uses a helper where available,
with OSC 52 as a terminal-dependent fallback.

The provider image formats follow the installed Codex schema,
[Codex app-server](https://developers.openai.com/codex/app-server/) and
[Claude streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).
