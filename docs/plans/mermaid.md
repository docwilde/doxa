# Mermaid in the Rust transcript

Status: **local preview, sandbox doctor, Settings preflight, bounded PNG cache and explicit offline setup implemented; pinned real CLI suite, VTE halfblocks and actual Kitty graphics passed on Linux**.

The native Ratatui transcript recognizes complete, standalone `mermaid` code
fences. Without explicit configuration, it renders the source fence exactly as
before. An owner can configure an absolute mmdc-compatible executable and a
dedicated package root in Settings. DOXA never downloads a renderer, discovers
one on `PATH`, or sends diagram text to a hosted service.

The renderer runs off the UI thread behind Linux bubblewrap with no network,
private input/output files, a five-second timeout, and bounded source and PNG
sizes. Its package root and system runtime are readable; the session workspace
and home are absent. A successful result adds a four-row terminal overview. The original fence
stays readable and copyable below it, including after successful rendering.
The actual halfblock trial showed that eight vertical color samples cannot
preserve diagram labels; the preview supplements the source. Text mode shows
only source. [Current controls and limits](../terminal-images.md#mermaid-fences).

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

## Acceptance and remaining limits

On 2026-10-20, the [explicit offline helper](../../scripts/mermaid-validation/README.md)
provisioned a dedicated 822 MiB package/browser from an existing reviewed npm
cache and Chrome copy. It uses the locked archive integrity, skips lifecycle
scripts, refuses existing destinations and never downloads Node/browser or
changes settings. The installed beta.63 doctor and pinned real CLI four-family
sandbox suite passed. The normal installer keeps the large dependency optional.

The production App rendered flowchart, sequence, class and Gantt fixtures in
real GTK3/VTE 0.84.0 on a private Xvfb display, at 125 columns × 33 rows with
DejaVu Sans Mono. Each produced halfblock cells; visual inspection confirmed
the readable source remains beneath the coarse overview. Source-only mode and
a narrower terminal are included in the local acceptance record.

A follow-up used authenticated Ubuntu Kitty 0.45.0 packages extracted only to
private task storage (9.9 MB download; no system installation). All four
families rendered in both actual KGP and `probe` modes under private Xvfb with
software Mesa. The trial caught and fixed two production bugs: a mode fallback
overrode successful negotiation, and forced graphics assumed 8×16 pixel cells.
Probed capabilities now survive; forced modes use actual terminal pixel
metadata or explicitly fall back to halfblocks when it is unknown. Real Kitty
replies reported 16×32 cells, and fixed renders used 128 pixels across four
rows instead of 64. Eight final screenshots were visually inspected; simple
flowchart labels are readable, while dense sequence/class labels remain small.

- Sixel and iTerm2 quality still needs their actual supported emulators. PNG
  decoding, VTE halfblocks and Kitty graphics cannot establish those results.
- macOS stays source-only. The production boundary requires Linux bubblewrap;
  a separate macOS isolation design and host validation remain open.

Graphviz translation and a hosted renderer are outside this slice. A partial
flowchart translator would not cover Mermaid's sequence, class, and Gantt
syntax; a hosted service would disclose private transcript content.
