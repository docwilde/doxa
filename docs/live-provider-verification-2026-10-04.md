# Native provider release verification — 2026-10-04

These bounded Linux checks used the **2.0.0-alpha.68** native daemon from
`0bd4fb7`, built with production features. The Claude CLI was **2.1.285**.
They used a private synthetic workspace and DOXA/LORE state on real disk;
LORE and peer tools were disabled. Only the normal DOXA Claude credential
isolation read the local subscription login. The guarded vendor launcher
resolved saved credentials and did not print them. No provider response,
reasoning text, nonce, account identity, or private path is retained here.

| Engine | Synthetic first turn | After native daemon stop/resume | Usage |
| --- | --- | --- | --- |
| Claude, account default model, low effort | Exact 8-character token; one text delta | Exact token; one text delta | Numeric input and output tokens reported on both turns |
| DeepSeek `deepseek-flash`, low effort | Read the synthetic file and returned its exact 16-character token | Recalled the exact token; paired two-turn history | Complete, model-consistent numeric usage on both turns |
| z.ai `glm-5.3-flash`, low effort | Read the synthetic file and returned its exact 16-character token | Recalled the exact token; paired two-turn history | Complete, model-consistent numeric usage on both turns |

Each check submitted exactly two short turns. Both vendor catalogs came from
the provider account; the verifier selected the low-cost listed model before
sending any request. The Claude catalog was available and its model was
reported before and after resume. All three checks returned `passed` and
cleaned up their owned processes and temporary state. DeepSeek and GLM
provided provider-reported token counts; their displayed costs remain DOXA
estimates, not independently verified bills.

To repeat the same bounded checks on a checkout containing the verifier,
use these commands. The measured alpha.68 daemon was built separately from
`0bd4fb7` before the Claude verifier was added.

```sh
# Set TMPDIR and CARGO_TARGET_DIR to private directories on real disk
# with a short Unix-socket path.
cargo build --locked -p doxa-daemon
cargo build --locked -p lore-core --bin lore-rs
DOXA_NATIVE_DAEMON="$CARGO_TARGET_DIR/debug/doxa-daemon" \
  python3 scripts/verify_native_claude_live.py --live
DOXA_NATIVE_DAEMON="$CARGO_TARGET_DIR/debug/doxa-daemon" \
DOXA_LORE_RS="$CARGO_TARGET_DIR/debug/lore-rs" \
  cargo run --locked -q -p doxa-vendors --example verify_native_live -- --live
```

The Claude verifier is opt-in, accepts no endpoint override, and emits only
the success fields above. The vendor Rust launcher uses DOXA's production
credential guard before isolating the child. The scripts are not CI tests
because they contact paid or subscription accounts.

This verifies basic Linux startup, native turns, resume and usage on these
exact versions. The one-delta Claude replies do not measure long-response
streaming fluency or thinking progress. Optional overage and model-specific
Claude quota variants were not observed. The authenticated Codex default-window
compaction result remains the separate [alpha.52 record](live-default-window-compaction-2026-09-30.md);
it was not rerun. macOS has no authenticated provider result from this run.
Windows is outside the current Unix transport and supervision architecture.
