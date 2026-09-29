# Native verification follow-up — 2026-09-29

This record covers the alpha.46 candidate and bounded authenticated checks on
Linux. The fixed daemon at `4fcb9ff` was a debug build with production features.
These are behavior checks, not a performance comparison or a claim that every
release, provider, platform or historical store has passed.

See the [native runtime guide](../rust/README.md),
[provider contracts](../rust/doxa-engines/README.md) and
[parity boundaries](rust-1.19-parity.md#preserved-boundaries).
The [2026-09-28 record](live-provider-verification-2026-09-28.md) retains earlier
versions, unsuccessful attempts and successful manual/controlled compaction.
All paid provider tests have stopped.

## Provider observations

| Path | Observed result | Remaining boundary |
| --- | --- | --- |
| Claude fixed debug daemon | Approximate thinking counts advanced **50 → 150 → 200 before the first text event**; the reply produced **104 text deltas**. Thinking plaintext stayed withheld. | These counts are estimates, not official token accounting. Optional quota/overage variants and arbitrary CLI builds are not established by this probe. |
| DeepSeek fixed streaming diagnosis | **Three turns passed** the bounded diagnosis, with **316 text deltas** and **81 live reasoning-progress events**. Native text is emitted incrementally through canonical secret scrubbing. | An earlier candidate failed an exact-nonce check. That failure remains unresolved; the later streaming diagnosis does not establish that earlier run's recall/resume correctness. |
| z.ai GLM fixed capability/streaming path | **Three turns passed**, with **408 text deltas** and a **low → high effort change on the same session**. | The evidence collector omitted numeric GLM usage. Usage is unmeasured in this record, not zero. |

The vendor transport uses the shared native stream scrubber, preserves secret
redaction across chunk boundaries and avoids emitting the completed response a
second time. Failed or uncertain streams do not commit replay history. Reasoning
plaintext remains quarantined until canonical scrubbing; live progress can use
approximate counts.

## Codex default-window automatic compaction

The bounded stress run stopped with **`incomplete_harness_cap`** after
**2,758,826 aggregate input tokens**. Its observed context was **241,119 tokens**,
below the approximately **244,800-token default automatic trigger**. The usable
model window was **258,400 tokens**. Aggregate input sums repeated requests and
must not be treated as the current context size.

There was **no default-window automatic compaction, no Haiku review for that
compaction, and no post-compaction restart verification**. The run is incomplete;
it did not verify the default trigger. Further paid testing is stopped.

Earlier manual compaction and automatic compaction with a deliberately lowered
**14,022-token threshold** passed real native Claude-backed LORE review and
post-compaction recall. That controlled test remains distinct from default-window
stress and does not fill this gate.

## LORE hub and historical replica

The persistent loopback `lore-hub` transport works. Complete historical replica
parity remains gated. A fresh isolated canonical replay of **457 owned portable
ops**, preserving their original signatures, produced **302 applied**, **154
unverified**, **one deferred**, and **zero failed**. Fixing portable pending UID
lookup removed the previous **46 `pending/resolve` invalid-request failures**.
No unverified operation was approved or re-signed during that replay.

Four signed oversized session snapshots have a bounded native receiver fix.
One unsigned oversized operation still cannot enter the unchanged pending-review
limit and blocks complete historical pull. The **154 unverified operations** and
**one deferred dependency** also remain unresolved. Working transport and a
successful portable subset do not establish full historical parity.

Canonical export transfers signed operation history, not a complete current-state
snapshot. Seed and re-sign do not replace immutable hub history or automatically
re-author represented foreign state. Full reconciliation needs reviewed canonical
state/provenance handling and a safe treatment of the blocking operation; trust
or class gates must not be disabled to claim success.

## Alpha.46 candidate changes

- Independently verified Codex rebuilds publish immutable artifact directories;
  an atomic pointer selects new launches and existing provider files remain
  intact. Corrupt installed artifacts or receipts are refused.
- Reported subscription usage becomes yellow above **66%**, red above **90%**;
  missing quota remains unknown.
- Mouse-wheel routing follows the hovered pane/control; memory previews wait
  until the intended selection is ready.
- Memory review views distinguish **Pending** and **Clustered** titles.
- Saved split layout can be restored offline without dropping saved conversations.

These implementation changes have focused regression coverage. They do not
change the provider or historical-replica verification boundaries above.

## Platforms and evidence handling

Linux is verified. The full protected DOXA runtime requires Linux ownership and
supervision primitives; macOS and Windows are unsupported by this architecture.
Standalone LORE on macOS remains unverified; Windows is unsupported.

Verification used isolated owned workspaces/stores and bounded process cleanup.
Public evidence contains counts and outcomes only: no credentials, account
identities, private thread IDs, source memory, prompts, replies or private proof
locations. The original stores were not mutated by the disposable replay.
