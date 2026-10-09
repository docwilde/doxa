# Images in the Rust transcript

The Rust TUI renders a standalone Markdown image with an absolute local path
inside the session's verified workspace,
for example `![Build graph](/home/user/project/graph.png)`. Its alt text stays
visible above the preview. Inline images, relative paths and remote URLs keep
their existing text/link rendering. The preview is local UI content; it does
not send image pixels to the model or fetch them from a network service.

`image_mode` in Settings chooses `halfblock` (the default, portable Unicode
cells), `probe` (detect Kitty, Sixel or iTerm2 graphics), a forced protocol
(`kgp`, `sixel`, `iterm2`), or `text`. A forced protocol needs a terminal
that implements it. `probe` asks the terminal for capabilities after entering
the alternate screen and can pause briefly. Text mode shows the alt label.

The TUI reserves four transcript rows per preview so scrolling and pane
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
width. Resizing back to a prior width can reuse its PNG. Changing renderer
settings, package identity or workspace discards the cache. A corrupt or
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

Provider binary attachments, screenshots pasted into the prompt and remote
image URLs still need separate input paths. Terminal protocol quality depends
on the emulator, tmux and SSH path; halfblock is the portable fallback.
