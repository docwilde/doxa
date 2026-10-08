# Mermaid in the Rust transcript

Status: **local preview slice implemented; real Mermaid CLI validation open**.

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

## Still open

- Validate a pinned Mermaid CLI, its Chromium installation, and terminal
  backends on supported Linux hosts. The current automated checks use a stub
  renderer and do not claim full diagram-type fidelity.
- Decide whether the installer should offer the large optional Node/Chromium
  dependency. It should state the cost before installation and never install
  Node silently.
- Add a useful `doctor` report for renderer availability and a settings
  preflight that explains sandbox startup failure without exposing source.
- Decide whether session-scoped persistent caching is useful. Current results
  live only in the TUI process; resize may rerun a diagram.
- Evaluate macOS isolation separately. The Linux sandbox requirement currently
  leaves Mermaid source visible on other platforms.

Graphviz translation and a hosted renderer are outside this slice. A partial
flowchart translator would not cover Mermaid's sequence, class, and Gantt
syntax; a hosted service would disclose private transcript content.
