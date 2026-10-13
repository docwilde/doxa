# Root isolation broker completion notes

2026-10-13: delegated scope read from common.md and isolation.md. Worktree starts
at parent 79fc36ea. Only new broker package files are owned here; native gateway,
Runtime integration and quota verifier are parent/peer work. No main edits, live
host provisioning, paid providers or Engine restarts are authorized here.

Contract proposed to parent: immutable root-installed scoped policy; framed Unix
RPC with scoped lifecycle/stdio; exact SO_PEERPIDFD Engine identity; no CLI/shell
fallback. Root-owned native composed verifier is required before ready/admission.
Worker wrapper syntax for workspace execution and native verifier interface are
pending coordination. Tests will distinguish protocol fixtures from live proofs.

Baseline committed a9acf694 before verification. Seventeen tests passed in
0.341 seconds; these prove protocol/negative checks and a local kernel peer pidfd
primitive, not live quota/Engine/provider containment. Parent froze shared v1
policy and quota layout. Parent accepted typed staging and a native stage-check
before/after gate. Native hook registration now forwards only fixed root status
messages, authenticating root gateway peer pidfd/executable/owned process.

Follow-up: provider-home staging aligns ROOT/home/{claude,codex}, with fixed
provider config environment. New files only, never overwrite auth/config files;
Engine-owned mode0600/0700 entries inherit installed project quota and need live
native before/after checks. Unit preserves host mount namespace because systemd
ProtectSystem/ProtectHome would change STATX_MNT_ID and invalidate quota identity.

Follow-up committed 32714f3c before verification; 27 tests passed. Read router
provider-home operation inventory. Retain existing auth, drain/hash-check incoming
bytes, atomically replace fixed bounded Claude settings after complete tree hash.
Narrow prepared home paths; runtime transcript directories are not staging inputs.
Parent required strict Engine socket0600 plus exact Engine GID; guest normalized
socket in place without Engine restart. Completed-nonzero native helpers now never
signal a reaped PID/group; meaningful real /usr/bin/false regression added. Thirty
tests passed in 0.378 seconds. Read-only guest Engine/negative-peer proof prepared.

Read-only disposable guest proof completed: Engine UID1001/PID27821,
SO_PEERPIDFD/executable/config pin, socket dev56/inode688 stable, UID1000 excluded,
same-Engine-UID fake Python peer rejected by executable hash. Exit0 evidence is
in evidence/peer-20261013.json; exact scope/command in evidence/RUN.md. Initial
negative fixture exited too early; added parent/child lifetime barrier and reran.
No Engine/container/config mutation or provider call; admissible_as_hardened=false.

Parent QA expanded final work: protect fixed Git builtin grammar against provider
alias bypass; clean resume/stop errors and lost create replies; allow scoped ROOT
explicit Engine restart transition; serve read-only controls concurrently with
long provider transport. Committed implementation c1e6f85e before follow-up checks.
Exact inherited image-label merge now independently derives from pinned image
ID metadata, fixing actual guest run1 mismatch. Added bounded provider snapshot
and verify RPCs, actual Engine source stat/SHA/thread/project guards, caller-private
snapshot/expect files. Coordinated flags/schema directly with isolation_quota
Root Rust client. Forty-two focused tests now pass; these include concurrent
control dispatch during a held provider fixture, alias/options denial, exited404
stop, failed resume cleanup, deterministic lost-create reconciliation, idempotent
prepared snapshot inputs, and post-review source mutation rejection. Production
native/CID/filter/gateway/provider proof remains coordinator/authority-owned.

Parent reopened for P1 Git filter execution and actual unprivileged ROOT client
proof. Actual UID1000 can obtain ROOT peer pidfd/cmdline/stat but opening ROOT
/proc/PID/exe fails EACCES13; no UID or ptrace permission relaxed. ROOT startup now
publishes immutable-root-writer mode0444 broker-instance.json containing actual
self executable/code/policy SHA, PID/pidfd PID/starttime/boot, argv/socket identity.
Caller verifies root-controlled public files + exact kernel ROOT pidfd/socket,
then a fresh nonce challenge on that same stream. Actual UID1000 CLI describe
against a real ROOT broker passed (PID45516, unprepared/CIDnull, no Engine API).
Proof script source and evidence/root-client-20261013.json retained. First fixture
setup failed because Ubuntu /usr/bin/false is a symlink; canonical ROOT artifact
path fixed, then proof passed. No production admission is inferred.

Git P1 follow-up: source .gitattributes can execute clean/smudge/process drivers
despite no-ext-diff/no-textconv. ROOT now constructs independent readonly Git
metadata on the existing broker quota bind, excludes all source config/hooks/
attributes/alternates, generates minimal config and supplies fixed environment.
Bounds align peer quota296959a9:4096 total descendants/depth24/128MiB-or-disk.
Status/diff ignore submodules; safe.directory is exactly/workspace. Ordinary
apply writes only worktree bytes; cached/index changes refused pending sync.
Actual UID1001 filter-marker guest proof prepared, no Engine/provider/quota writes.
Lifecycle regressions cover early helper failure cleanup, old/new CID handover,
and helper teardown exceptions still reaching force-delete/confirmed absence.
Typed guard now permits native live per-turn checks during persistent provider
transport, without caller-mirror disk samples or invented usage counters.

Baseline86ff32bf committed before tests;48 focused tests passed in0.575s.
Guest marker proof20508042 passes actual Git2.53.0 as UID1001: unsafe clean/
smudge/process markers reproduced, eight safe operations produced none; original
index unchanged, ordinary apply changed worktree, ROOT readonly21-entry/663-byte
metadata checked. No Engine/quota/provider writes. Initial fixture lacked its
session_id; fixed and reran; both private scopes cleaned. Parent review requires
live ROOT /proc/PID/stat starttime equality before/after fresh client challenge;
added exact checks plus mismatch/reuse regression and updated actual-client proof.

Starttime baseline2766a54d:49 tests passed0.576s. Actual client reproof18a45886
passes PID47166/starttime856669 with UID1000 live-stat equality, exeEACCES13.
Tar-preserved source UID/groupwrite initially failed ROOT setup; normalized only
unique guest package toROOT0:0/0644 and ensured setup failure fixture cleanup.
P2 additional fix: absent-CID resume also retires old gateway/adapter handles via
independent cleanup before handing ownership to fresh prepare; uncertain helper
retirement prevents fresh scope creation even when the old container is absent.

Final code baselinef26a69b7:50 focused tests passed0.577s, clean status and
diff-check. Evidence and exact commands are committed in evidence/RUN.md.
Native composed admission proof, Runtime wiring and production provisioning
remain parent/authority-owned; these Git/client fixtures make no readiness claim.

Parent review reopened a real concurrent audit race: in-place0700/0600 Git view
construction/deletion could make an independent live native gateway reject quota
metadata. New design builds under ROOT/.git-staging ROOT0:0/0700 on the same
quota filesystem outside binds; FSGETXATTR verifies inherited project on every
temporary inode. Whole readonly generations publish/retire by rename under a
fixed ROOT runtime600 metadata flock. Native takes shared lock around all quota
audits; ROOT exclusive lock covers only publication/retirement, not slow copy or
recursive removal. SourceRoot audit ignores extra Root siblings but kernel hard
limit charges staging. Meaningful real-flock slow-build/held-reader retirement
and deadline/invalid lock tests added; native composition owned by parent.

Atomic sourcef7745094 committed before verification. Initial UID1000 unitfixture
lacked ROOT DAC override for cross-parent readonly directory rename; corrected
fixture-only rename emulation3b76635a,53 tests passed0.698s. Actual ROOT guest
proof4b5690b1 passes without that emulation: intentionally paused private0600
copy, actual shared-lock audit counts[0x10,1,0], held-reader retirement blocks,
Root600 lock validated, eight actual UID1001 Git operations no markers/index
unchanged. Updated UID1000 client proof also passes RootPID55863/start1159695,
exeEACCES13. No Engine/provider/quota writes or admission claim. Both private
data/runtime scopes cleaned; versioned ROOT proof packages/evidence retained.

Parent source review found nonempty lock acceptance and global state-lock guard
starvation during private copy. Narrow source fix requires zero-byte lock plus
full held/fresh identity across acquisition/operation. Workspace reserves exact
CID/Engine and active count before releasing state lock for copy; scoped/native
revalidation precedes short publication and exec, with state->shared->EX lock
order. Meaningful held-copy guard responsiveness and changed-scope refusal
regressions added. Frozen running guest broker/policy bytes were not changed.

Narrow final source236c0dbd:55 focused tests passed0.733s, clean status and
diff-check. Broker SHA256 is4ff9a871df8eab03fd4be94693564c3d4d7ef3f21470b040b5e82068ee7a81d9.
Parent will reprove these final bytes in a fresh composed scope. Earlier actual
atomic/client guest evidence331355b7 remains explicitly tied to prior source;
no frozen guest policy, broker or global Engine state was changed for this fix.

Composed5012/r4 independently confirmed exactCID404/kernel sockets/processes0
but four stale physical native sockets after30-second prepare refusal. Narrow
production fix pins ROOT/broker hook+family egress socket identities and runtime
status identity while owned gateway is alive, then descriptor-relative removes
only matching leaves after captured gateway PIDFD exit. Replacement/unknown
socket/parent/mode/link errors preserve refusal and cleanup-required state.
Native helper/service stderr now drains concurrently into8192-byte memory
prefix, discards flood overflow, and records actual exit plus limited reason in
three fixed Root600 diagnostic files. Provider stdout/stderr/environment is
separate; diagnostics never admit a session. Meaningful socket replacement,
unlink error, live PIDFD, stderr flood and absolute deadline regressions added.
Only source/tests/docs changed; frozen4ff9 broker and guest5012 remain untouched.

Cleanup/diagnostic baseline002a4d2b:62 focused tests passed0.822s. Main review
required guarding all fallible post-Popen setup before any pipe mode/PIDFD/
drainer allocation, removing implicit unbounded context-manager waits. Actual
benign-child injected setup failure tests now verify bounded kill/reap and pipe/
PIDFD closure. Parent also confirmed Runtime.stop→explicitRootresume contract:
same ROOT manifest policy/SID/boot/current authority, owned helper retirement,
oldCID404 and controlled Engine pin precede fresh CID; data is retained without
staging, while cleanup-required/stale scope stays refused. All edits remain
source-only; running guest broker/policy/Engine state were not changed.

Final code0eed8db9:67 focused tests passed0.830s; clean status/diff-check.
Broker SHA256 a33f76cde9a64297cacbecff64862b4db8db65ed036633bfe163ea682fcd5638.
Actual child injection covers helper pipe setup, gateway PIDFD allocation and
stderr drainer allocation failures with bounded kill/reap and descriptor closure.
Fresh composed5013 is coordinator-owned and pending review/freeze; prior actual
guest evidence remains tied to its exact earlier source, with no promotion of
failed/mutable fixture receipts or altered frozen5012 artifacts.

Independent review reopened uncertain native child teardown: failed bounded
kill/reap could clear gateway ownership and helper pipes despite a live child.
New private helper ledger retains Popen/PIDFD/remaining descriptors; gateway
retains its process/PIDFD/drainer/socket pins until verified exit and scoped
endpoint cleanup. Admission/lifecycle is quarantined; stop/cleanup retries only
retained pins, still removes exactCID, and preserves cleanup-required until all
owned helpers close. Quiesced shutdown retains ownership across bounded retries.
Stopped manifest version now requires exact integer1, including initial load
and prepare, refusing JSONtrue/1.0 before normalization. Added actual benign
child injected teardown-failure/retry regressions plus lifecycle quarantine and
malformed stopped-version fixtures. Source-only; guest5013 remains held and
frozen5012 broker/policy/Engine artifacts remain untouched. Committing before
focused verification as required by the isolation skill.

Retention source9424e586 + fixture correctiond3ec2f49:72 focused tests pass
0.834s, clean diff-check/status. Initial run exposed the existing concurrent
server Mock's unspecified quarantine method returning truthy indefinitely;
fixture now explicitly models no native quarantine. Actual benign-child
tests independently verify live helper/gateway process and PIDFD retention,
open pipes/drainer, refusal of new admission, repeated uncertain teardown,
confirmed retry reap and descriptor closure. CID removal/404 still occurs on
helper failure without issuing stopped until the retained helper is reclaimed.
Broker SHA256 a04d2d5d0fb3df93cb3201d2d3e1cb322829ed4ddc5d52938e17fe0a95a0056f.
No guest commands or live artifacts were changed; final5013 composition is
pending independent review and coordinator freeze.

Actual composed5013/R5 reached Runtime/ready/activate/hook/protectedCodex/guard/
snapshot/private copy/refusal checks, then RootGit status refused a newline-only
packed-refs file. Peer inspected retained source bytes: exactly header-only
`# pack-refs with: peeled fully-peeled sorted ` plus newline. Sanitizer dropped
the header but unconditionally appended newline to an empty row set. Narrow
source fix emits empty bytes for an empty sanitized set, preserving validated
heads/tags/peeled rows. Actual local Git readonly-view regressions cover empty,
exact R5 header-only, fully filtered remote/peeled inventories, and normal packed
heads/tags/annotated peeled refs; fixtures substitute ROOT/quota metadata only,
and do not authorize live isolation. Frozen R5 broker/policy/artifacts untouched;
no guest edits. Committing before focused verification.
