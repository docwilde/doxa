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

Only standalone local Markdown attachments are covered. Provider binary
attachments, Mermaid fences, screenshots pasted into the prompt and arbitrary
remote image URLs still need separate input/rendering paths. Terminal protocol
quality also depends on the emulator, tmux and SSH path; halfblock is the
portable fallback.
