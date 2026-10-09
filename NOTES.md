# Code graph verification follow-on

- Base: `feat/beta36-followons` at `1ee7775a`.
- Scope: fail closed when Git root lookup or whole-worktree file enumeration floods, stalls, exits with an error, or produces an incomplete NUL-delimited path list. The bound applies to fresh queries, read-only cache validation, and stored snapshot whole-scan readback through the shared `doxa-codegraph` functions.
- Limits: ten seconds for each Git subprocess, 4 MiB for file enumeration stdout, 4 KiB for root lookup stdout, and 16 KiB for stderr. A failed subprocess is killed with its process group before waiting.
- Adversarial checks: normal NUL-delimited output, stdout flood, stderr flood, closed pipes with a running process, a child that exits while a descendant holds a pipe, and nonzero exit after plausible partial output.
- Remaining: source-file reads and the initial executable spawn can still block before the elapsed-time checks return. Whole-worktree byte observations remain non-atomic. Semantic `binding` remains `unknown` until the reviewed Engine and broker linkage described in the semantic verification plan is proven.
