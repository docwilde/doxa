# Queued ACK handoff continuation on 2026-10-20

The installed beta.64 base is `d9a4c12d`. This record covers source corrections
and deterministic daemon/tool fixtures. No new paid fleet was launched for this
continuation; the earlier $0.477688 conservative allocation holds remain
unchanged. The previous real worker's false claim of an unsent handoff remains
a failed live observation, as recorded in `fleet-readiness-2026-10-20.md`.

A canonical ACK can queue a new peer-origin turn while the sender's handoff turn
is still running. A real runtime fixture reproduced work turn N, owner handoff
turn N+1, then ACK/confirmation turn N+2. The old release rule accepted only N+1,
so it refused this ordinary two-turn exchange even when all three admitted
messages agreed.

The correction retains the existing N+1 path and adds a narrow continuation.
For N+2, the host must observe successful work, handoff and confirmation turns;
the exact admitted handoff, ACK and confirmation IDs; the coordinator's canonical
ACK-origin metadata; and successful native tool results for the actual sends.
The handoff turn must have owner origin. Only bounded peer discovery/history
calls may accompany each required send. Workspace, shell, MCP, unknown, failed,
missing or excess actions refuse the continuation. A later N+3 turn cannot reuse
it. These checks describe provenance, not model alignment quality or test success.

Native action receipts travel through a one-shot host capability after execution,
separately from provider event fields. Runtime strips provider/CLI attempts to
supply such a receipt. The receipt is limited to 8 KiB and 25 actions; overflow,
poison or missing capability leaves the new release path unavailable. Existing
providers without this proof retain the legacy path only.

The source binding uses the bounded offline Docker capture, including ignored
files, and compares its content digest again during owner review and release.
A source digest is frozen once per eligible completed predecessor work turn;
periodic idle reviews do not repeatedly scan it. Capture failure or its limits
(4,096 paths, 8 MiB per file, 128 MiB total) refuse this continuation rather than
fall back to names-only Git evidence. Native or online worker profiles do not
acquire this new proof merely from an unchanged diff.

The owner review token binds the continuation receipt and current completed
turn. Tests changed an otherwise valid prior receipt after review and verified
that the stale token refused release. Changed source, arbitrary replacement
snapshot hashes, fake send UUIDs, wrong ACK origins, failed/missing results,
unrelated owner/work actions and later turns also refused. Restoring the exact
reviewed evidence allowed the deterministic fixture dependency release.

Validation passed: 41 fleet-control tests (including the real queued-turn daemon
and review/release mutation cases), seven native peer-gate tests, the isolation
wrapper's one-shot forwarding regression, 32 fleet unit tests, 14 fleet policy
tests, six reviewer-example tests, 11 runtime unit tests and 28 runtime protocol
tests. Candidate frontend and daemon builds passed. These are local checks;
protected integration CI and a successful provider-backed handoff remain separate
acceptance requirements.
