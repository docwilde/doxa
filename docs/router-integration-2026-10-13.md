# Router integration verification — 2026-10-13

This record covers reviewed automatic compaction and configuration/allowance
inheritance added after beta.50. It records credential-free local integration
evidence. It makes no real-account, billed-price or task-quality claim.

## Behavior under test

- The optional frozen `auto_compact_percent` policy uses the existing canonical
  LORE source review and bounded API summary path before the ordinary worker.
  It retains one daemon terminal boundary, original transcripts, separate summary
  identity, usage settlement and uncertain-call holds. A CLI summary target is
  refused without an implicit API switch.
- Human-reviewed children receive a durable disjoint share of the parent's
  remaining dollar, routing-call and routing-spend allowances. Parent journals
  retain child ceilings; nested children can allocate only within their share.
  Admission verifies exact target, config and native permission. Stale reviews,
  competing allocations and failed durable saves cannot create a fresh budget.
- Router fleet admission freezes private per-slot configs and splits routing
  limits across slots, alongside existing disjoint run-budget shares. Startup
  and controller recovery verify daemon digests and ceilings. Recovery does not
  redeliver admitted prompts.
- Historical launches recover the original private config path, decision
  service and ceiling. Environment and Settings changes do not reset allowance.
- Docker launch prepares each configured CLI family before activating the
  container transport. Router migration plans include the complete canonical
  host journal fingerprint and preserve logical project identity, config,
  selection, permission and durable allowance. The paired isolation changes
  validate that checkpoint before teardown and before workspace mutation.

## Process fixtures

The child fixture makes an actual routed API worker request `spawn_session`.
It reviews the exact delegated task, observes the parent's in-flight worker
reservation, and starts a separate daemon and Git worktree. Parent and child
retain one canonical Git project but separate transcripts, worker identities and
journals. The child completes its approved dummy task through a loopback provider.
The parent resumes with the child ceiling still reserved.

The fleet fixture starts two actual Router daemons through the frontend launch
path, kills only the observing controller after dispatch, waits for verified
worker accounting, and resumes the controller. The two sessions retain their
original per-slot routing limits and dollar ceilings. Both worker journals keep
one turn, and the provider fixture records exactly two worker posts before and
after recovery. No prompt is redelivered.

The CLI fixtures use fake authenticated catalogs and private provider UUIDs.
They cover explicit CLI pin/last-worker compaction refusal, native permission
and config changes during human spawn review, native command/peer permissions,
provider switching, source mutation, interruption and missing usage. The fake
account data is not authenticated-account evidence.

Automatic summary fixtures cover success, restored context, review disabled or
denied, review cancellation, exact source changes, known rejected-summary usage,
unknown usage, request caps, interruptions and retained crash reservations. They
preserve the same source-review contract used by explicit `/compact`.

## Validation and limits

Focused checks use two build workers, a 12 GiB memory scope and a private short
real-disk temporary root for Unix sockets. No paid model calls or owner install
is performed by this verification.

| Focused target | Result |
| --- | --- |
| `doxa-router --test core` | 8 passed, including exact decimal/scientific USD ceilings and sub-micro flooring |
| `doxa-daemon --features local-test-server --test router_compaction` | 13 passed, including actual reviewed child and two-slot fleet/controller recovery |
| `doxa-daemon --features local-test-server --test router_host` | 66 passed, including failed saves, competing allocations and migration journal tampering |
| `doxa-daemon --features local-test-server --test router_cli_workers` | 8 passed, including stale config/permission during human review and CLI-summary refusal without an implicit API switch |
| `doxa-tui --lib router` | 30 passed, including isolated original config/Snap/ceiling restoration and automatic progress |
| `isolation_migration::tests::snapshot_relaunch_preserves_scope_lineage_and_provider_choice` | 1 passed, including Router config and both CLI executable choices |
| Production `cargo check -p doxa-daemon -p doxa-tui --bins` | Passed without the local-provider feature |

The decimal ceiling fixture covers values that previously lost one micro through
a floating-point round trip. Parent-to-child and history launch paths render
the exact original decimal ceiling; no allowance is rounded upward. The saved
config fixture runs in an environment-cleared subprocess with a mode-0700 home,
so credential guards are exercised without accessing the owner's vault.

The disposable Docker frontend fixture is extended to Router clean/dirty
workspaces and target-start rollback, with byte-identical Router journals. It is
an explicitly ignored host-dependent check and must run after integrating the
paired isolation branch and supplying a reviewed disposable Engine/image.
Native CLI/provider transport fixtures do not establish Docker worker origin or
hardened admission. Those remain governed by the separate isolation proof.

Finite dollar ceilings apply only to tracked Router/API charges. CLI and external
MCP provider charges remain unknown. Child allocations are retained after failed
startup; automatic reclaim is deliberately unavailable. Canonical source and
request sizes remain bounded, and real-task summary quality remains unmeasured.

## Bounded installed-build pilot entry points

These entry points are for the parent after all six changes are merged and the
combined build is installed. They have not been run against real providers in
this branch. The parent chooses and reviews the exact candidates, account
catalogs, prices and private configs. Config files must be absolute, owned, mode
0600, and outside shared or symlinked directories. Keep reviewed output caps
small; changing a config after admission invalidates its recorded identity.

For a small first pilot, freeze `max_calls: 4`,
`max_spend_usd_micros: 10000`, `max_input_bytes: 4096` and `deadline_ms: 1000`.
The decision allowance is $0.01. Make separate configs with
`router_model: "jev-1.13.0"` and `router_model: "subq-snap-preview"`; preserve
all other reviewed policies. Omit `auto_compact_percent` for these one-turn
transport pilots. A session ceiling of $4 and a two-slot fleet run ceiling of
$4 bound tracked decision/API charges; each fleet slot receives $2, two routing
calls and $0.005 of routing spend. They do not bound CLI subscription/provider
charges or external MCP calls. Use API-only candidates for the fleet money
check, and a separately reviewed CLI candidate for the file task.

Run these installed commands from a disposable Git repository. Substitute the
parent's private absolute config paths:

```sh
DOXA_SESSION_BUDGET_USD=4 doxa new --engine router --model auto --router-config /private/jev-pilot.json --isolation native --linger 120
DOXA_SESSION_BUDGET_USD=4 doxa new --engine router --model auto --router-config /private/snap-pilot.json --isolation native --linger 120
```

Submit exactly one task to each: `Return the integer result of 7 times 13. Do
not use tools, spawn sessions, send peer messages or request another turn.`
Inspect the routed event and journal receipt for the decision service, exact
selected target, known decision/API usage and `turn: 1`. A fallback receipt is
an observed fallback, not evidence of a successful real routing decision.
Use a five-minute operator deadline; on expiry interrupt through the attached
session, then stop it. `--linger` only bounds an idle daemon, not an active call.

For the real CLI worker file task, use the exact reviewed candidate ID as
`--model` (for example the parent's `codex-pilot` ID). This explicitly pins the
CLI worker and bypasses the decision call:

```sh
DOXA_SESSION_BUDGET_USD=4 doxa new --engine router --model codex-pilot --router-config /private/cli-pilot.json --isolation native --linger 120
```

Submit once: `Create only doxa-router-pilot.txt in this worktree with the exact
contents DOXA router pilot followed by a newline. Read it back, report its
contents, and stop. Do not commit, access credentials, spawn sessions or send
peer messages.` Review the exact native command/file request in the session.
Check the actual worker worktree and the canonical assistant result; the
original repository must stay unchanged. Record the private worker identity,
one CLI turn and the continuing unknown CLI money scope. Apply the same
five-minute operator deadline and interrupt/stop controls.

The two-slot routing/recovery pilot uses the existing fleet frontend, manifest
and RPCs, exactly as the process fixture above. Review with `--dry-run` first;
then start once with the same arguments, omitting `--dry-run`:

```sh
doxa fleet start --pool router:auto --router-config /private/jev-pilot.json --sessions 2 --run-budget 4 --cwd /private/disposable-repo --root /home/you/.dt/rf --run-id router-pilot-jev --isolation native --approve none --approval-grace 60 --message-review off --review-budget 0 --quiet-dwell 60 --quiescence-timeout 300 --prompt 'Return only the integer result of 7 times 13. Do not use tools, spawn sessions or send peer messages.' --dry-run
doxa fleet status router-pilot-jev --root /home/you/.dt/rf
```

Verify two Router assignments and no alignment supervisor in the dry-run. A
saved alignment-supervisor setting requires a positive separate review budget;
the explicit zero budget here refuses that configuration before launching.
Use the existing Settings flow to choose the intended pilot policy, then repeat
the dry-run. The routing transport pilot does not require an alignment model.

Capture the PID of that `doxa fleet start` controller (for a background launch,
`$!`). Wait until its private `router-pilot-jev/manifest.json` has
`phase: "monitoring"` and both slots show the dispatched prompt. Kill only that
captured controller with SIGKILL; keep both daemons alive. Wait at most five
minutes for both `$DOXA_HOME/router/SESSION_ID.router.json` journals to show
`incomplete: false`, no pending CLI/compaction, and known decision/API
accounting. Record each slot's session ID, config hash, exact micro ceiling,
`turn`, `router`, `worker`, receipts and canonical task count. Then run:

```sh
doxa fleet resume router-pilot-jev --root /home/you/.dt/rf
doxa fleet status router-pilot-jev --root /home/you/.dt/rf
doxa fleet stop router-pilot-jev --root /home/you/.dt/rf
```

The recovery oracle is unchanged session/config/ceiling identities, one
canonical user task and one worker turn per slot, unchanged routing/worker
accounting and no additional worker-start receipt. The local fixture also
counts exactly two provider posts before and after recovery. If accounting
remains incomplete or unknown, stop the existing run without redelivering the
task. Repeat the same bounded pilot with the reviewed Snap config and a fresh
run ID only after inspecting the Jev result.

If a fleet asks for human input, use `doxa fleet review RUN SLOT REQUEST_ID
--root ROOT`, inspect the complete request, then supply its exact returned token
to `doxa fleet answer RUN SLOT REQUEST_ID TOKEN '{"decision":"allow"}'
--root ROOT`. The existing `DaemonClient::connect` performs hello plus attach;
its `call("get_state", ...)`, `call("get_settings", ...)` and
`call("answer_needs_input", {id, answer, reviewed_request})` are the same bounded
transport used by the fixture. No additional control plane is needed.
