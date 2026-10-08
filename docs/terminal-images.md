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

Each diagram is limited to 16 KiB of source. A worker runs the renderer for at
most five seconds in a private directory, with network and host filesystem
access removed. It sees only its package, system runtime files, and its input
and output. A failed or timed-out render leaves the original fence in place;
no hosted renderer is called. Completed PNGs use the same 8 MiB and 8 million
pixel decoder limits as local images. At most two renders run at a time, with
eight results cached in the TUI process. A resized pane may render again.
`image_mode=text` always shows source. This path has stub-renderer coverage;
full Mermaid CLI and a graphics terminal have not been validated here.

Provider binary attachments, screenshots pasted into the prompt and remote
image URLs still need separate input paths. Terminal protocol quality depends
on the emulator, tmux and SSH path; halfblock is the portable fallback.
