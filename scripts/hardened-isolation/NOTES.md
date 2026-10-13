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
