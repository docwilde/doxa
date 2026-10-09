# Code graph source-basis follow-on

- Base: `feat/beta37-boundary-followons` at `93b67fed` in an isolated worktree.
- Scope: the disabled semantic broker stream observation now requires the Rust scan-input SHA-256 from the originating complete syntax query. It checks that digest against a fresh bounded inventory before and after the exchange. The challenge hash and opening/closing packet receipts carry the digest.
- Adversarial checks: a third Rust file edited during the exchange, changed before the challenge, or added during the exchange fails; a mismatched or missing scan digest fails; opening and closing packets with the wrong digest fail.
- Trust claim: a non-atomic observation of Git-listed, nonignored Rust source inputs only. Ignored files, manifests, configuration, other mounted bytes, and changes restored between reads remain unproven. A fake broker can echo every field. Semantic `binding` remains `unknown`.
- Guest gate: the offline QEMU fixture source now speaks the expanded packet format, but the retained six-case guest receipt predates it. This host lacks a readable guest kernel image and static BusyBox executable, so the updated guest run remains open.
