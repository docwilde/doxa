# Mermaid in the Rust transcript

Status: **local preview, sandbox doctor and Settings preflight implemented; pinned real CLI harness added, host run open**.

The native Ratatui transcript recognizes complete, standalone `mermaid` code
fences. Without explicit configuration, it renders the source fence exactly as
before. An owner can configure an absolute mmdc-compatible executable and a
dedicated package root in Settings. DOXA never downloads a renderer, discovers
one on `PATH`, or sends diagram text to a hosted service.

The renderer runs off the UI thread behind Linux bubblewrap with no network,
private input/output files, a five-second timeout, and bounded source and PNG
sizes. Its package root and system runtime are readable; the session workspace
and home are absent. A successful result becomes a four-row terminal image.
During rendering, on failure, or in text mode, the original fence stays
readable. [Current controls and limits](../terminal-images.md#mermaid-fences).

`doxa doctor` now checks configured paths against the preview policy, probes
bubblewrap/user namespaces, and renders a fixed PNG with a bounded decoder.
Its success confirms the sandbox command works with that executable; it does
not establish Mermaid CLI diagram fidelity or terminal display quality.
In Settings → Appearance, select either Mermaid renderer field and press
**Ctrl+P** to run that same preflight against the proposed values before saving.
It runs off the UI thread and shows a static, source-free failure reason for
path policy, bubblewrap, user namespaces, renderer startup, timeout or invalid
PNG. It uses only a fixed sample diagram; no transcript source enters the
preflight. A changed setting invalidates the displayed result. Saving remains
explicit, and a failed preview still leaves the source fence visible.

## Still open

- Run the [pinned real CLI harness](../../scripts/mermaid-validation/README.md)
  after explicitly provisioning its reviewed package root. This host has Node,
  bubblewrap and browsers but no installed Mermaid CLI; flowchart, sequence,
  class and Gantt fidelity remain unverified. A `TERM=dumb` agent terminal
  also cannot validate Kitty, Sixel, iTerm2 or on-screen halfblock quality.
- Decide whether the installer should offer the large optional Node/Chromium
  dependency. It should state the cost before installation and never install
  Node silently.
- Decide whether session-scoped persistent caching is useful. Current results
  live only in the TUI process; resize may rerun a diagram.
- Evaluate macOS isolation separately. The Linux sandbox requirement currently
  leaves Mermaid source visible on other platforms.

Graphviz translation and a hosted renderer are outside this slice. A partial
flowchart translator would not cover Mermaid's sequence, class, and Gantt
syntax; a hosted service would disclose private transcript content.
