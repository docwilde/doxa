# Root session broker

`root_broker.py` is an administrator-installed Python 3 program, executed with
`python3 -I`. It is the sole session Engine client. The caller and rootless Engine
must have distinct non-root UIDs; the Engine socket must exclude all other users.
There is no Docker CLI, shell fallback, generic Engine API or caller-selected host
path, image, endpoint, namespace, mount or resource policy.

The coordinator-owned native `doxa-hardened-gateway` must implement `serve` and
`broker-check`. The native checker independently proves live installed quota,
worker filter, Engine identity, container/kernel origin and a root gateway nonce
challenge. Broker fixtures do not prove those properties. A native check must
return `doxa-root-session-authority-v1` with exact session policy hash, CID,
Engine PID, boot, phase and family, plus `ready`, `quota_ready`, `filter_ready`,
`gateway_ready`; provider execution also requires `hook_ready`. Missing or false
facts fail closed. Root-native provider hooks are a reserved seam until composed.

The strict v1 policy has `version`, `session_id`, `caller_uid`, `engine`, `root`,
`image`, `worker`, `providers`, `authority`, `broker`, `limits`. The executable
validator is the schema source. Root installs a new immutable mode-0444 policy;
normal IPC cannot install, update or widen it. `engine` pins UID/GID, socket,
root-owned executable path/hash and read-only explicit config path/hash, API
version. Providers pin fixed CLI paths, complete image artifact hashes, exact DNS
egress, family port and a tiny environment name list. Limits are finite memory,
CPU quota/period, PIDs, aggregate disk, output, stdin and absolute deadlines.

Quota mounts are derived exclusively from the fixed ROOT: `checkout` →
`/workspace`, `home` → `/home/doxa`, `cache` → `/work-cache`, `broker` →
`/run/doxa/session` read-only. Root provisions every scope with the installed quota
policy. Root-private `manifest.json` and the root gateway status socket are under
`/run/doxa/hardened/SESSION`; worker-visible family egress sockets are in
`ROOT/broker`. Every init and exec uses a fixed image worker filter entrypoint:
`hold-hardened`, `probe-hardened`, `filter-probe`, `exec-hardened`,
`workspace-hardened`, `egress-proxy-hardened`. Workspace argv are Git arguments;
the worker owns the fixed Git program. Docker default seccomp remains enabled.

RPC begins with a 4-byte big-endian JSON length and
`{"v":1,"session_id":"...","op":"..."}`. Only `describe`, `prepare`, `resume`,
`stop`, `guard`, `provider_exec`, `workspace_exec`, `register_hook`, `unregister_hook`,
`provider_snapshot`, `verify_provider_snapshot` exist. Exec adds `argv`, optional
workspace-relative `cwd`, optional selected `env`; provider exec adds `family`.
Exec and staging following frames are 1-byte tag + 4-byte big-endian length + bytes. Tags:
1 JSON reply, 2 stdout, 3 stderr, 4 exit JSON, 5 refusal JSON, 6 stdin, 7 empty
stdin EOF, 8 empty cancellation. A root-pinned client preserves raw provider
stdio and keeps secrets in selected environment values over IPC, never argv.

The client reads the fixed root-owned mode-0444 provisioning receipt, verifies
root-owned code/policy/interpreter hashes, connects to the root-owned socket,
obtains `SO_PEERPIDFD` and verifies a root-owned mode0444 `broker-instance.json`
against the exact live ROOT PID/pidfd, socket inode, immutable artifact/policy
hashes, boot, start time and isolated argv. ROOT computes its actual executable
and script hashes when publishing this instance. A fresh nonce challenge on that
same peer stream binds the response to the instance bytes and live process.
The caller checks readable live `/proc/PID/stat` start time against that instance
before and after the challenge, bracketing possible PID reuse.
Unprivileged clients need no cross-UID `/proc/PID/exe` read; the actual UID1000
guest proof records EACCES13 there and successful client authentication. Root
writer control on every ancestor, policy, receipt, instance and endpoint excludes
same-UID forged JSON or fake brokers. Engine uses `SO_PEERPIDFD` too, pins daemon socket inode and
read-only config, refuses instance replacement, and performs exact CID inspect,
exec config inspect and HTTP upgrade on the same pinned Engine connection.

Install on a reviewed disposable host first; do not enable on the workstation as
a development side effect. Example administrator commands, after root provisioning
of quota trees/runtime directory and native authority:

```sh
/usr/bin/python3 -I /usr/libexec/doxa/root_broker.py install --source /etc/doxa/hardened-staging/SESSION.json --destination /etc/doxa/hardened/SESSION.json
/usr/bin/python3 -I /usr/libexec/doxa/root_broker.py serve --policy /etc/doxa/hardened/SESSION.json
```

The caller uses the same installed script in `client` mode:

```sh
/usr/bin/python3 -I /usr/libexec/doxa/root_broker.py client --session-id SESSION --op prepare
/usr/bin/python3 -I /usr/libexec/doxa/root_broker.py client --session-id SESSION --op workspace_exec -- status --porcelain
/usr/bin/python3 -I /usr/libexec/doxa/root_broker.py client --session-id SESSION --op provider_exec --family claude --env ANTHROPIC_API_KEY -- --output-format stream-json
```

Cancellation, disconnect, stream overflow or deadline removes the exact container
and confirms 404 before marking stopped; uncertain cleanup is recorded and
refused. Removal terminates all provider/adapter descendants. A cold broker
resume first rechecks the previous exact scope with native authority, then removes
it and prepares a fresh CID over the retained quota trees. No untracked sidecar
is adopted. Engine replacement remains a root reprovisioning boundary.

The [Docker Engine API v1.51 reference](https://docs.docker.com/reference/api/engine/version/v1.51/)
provides the scoped create/inspect/exec/archive operations and multiplexed stdio
format. The broker adds identity and deadline checks beyond the API transport.

Run protocol/security tests with `python3 -m unittest discover -s
scripts/hardened-isolation -p 'test_*.py'`. These include real local Linux peer
pidfd identity checks, slow-header/body deadlines, exact-FD exec sequence and
negative policy/inspect/cancellation cases. Live composed acceptance remains a
separate disposable guest proof.

New-session `prepare --checkout CLIENT_TREE` and selected `--home-file RELATIVE
CLIENT_SOURCE` inputs are read by the caller-side helper. Root receives typed
frames 9 (entry metadata), 10 (file bytes), 11 (entry count/inventory SHA), checks
all file hashes and writes through no-follow directory descriptors under fixed
quota scopes. The native `stage-check --policy ... --phase before|after --family
none|claude|codex` verifies live installed project quota before and after upload.
Staging refuses links/special files, path escapes, duplicate entries, more than
20,000 entries, oversized individual files, disk-budget bytes or slow transfers.
Provider input paths follow the reviewed inventory: Codex `auth.json`; Claude
credentials, settings, logout marker, prepared skills/adopted-plugin snapshots.
Existing auth stays intact. Incoming auth/settings must be bounded JSON objects.
Settings replace atomically after full inventory verification. Prepared snapshots
are at most 64 MiB, 4 MiB/file and depth 24, excluding Git/hooks/MCP inputs.
Provider runtime histories remain opaque; generic home copying/deletion is absent.

`register_hook`/`unregister_hook` use explicit caller daemon PID and an environment
name holding the capability. Registration also names its private caller-owned
callback socket. Root forwards only this typed scope to the pinned native root
status peer, which must validate caller UID/pidfd and fresh nonce/CID/policy facts.
The native hook is responsible for exact original transcript byte/inode rechecks.
Explicit logout/deletion, adopted snapshot replacement and stopped transcript
migration still require separately reviewed typed operations; staging does not
silently add a general filesystem writer.

`prove_broker_guest.py` exercises the actual pinned Engine `/info`, distinct UID
socket exclusion and rejection of a same-Engine-UID fake executable on a private
guest. It changes only its unique negative-peer fixture socket and reports
`admissible_as_hardened:false`; no container/provider/origin/quota success is
inferred from it.

Control requests run in at most eight bounded handlers. Exactly one provider
transport is admitted per session; approved Git transports and read-only provider
snapshots/hook controls can run while it is active. Lifecycle changes and staging
require an idle session. Git is limited to `status`, `diff`, `rev-parse`,
`merge-base`, `ls-files`, `apply`, with a reviewed option grammar. User global
options/aliases, external diff/textconv, unsafe paths and executable overrides are
refused. ROOT creates bounded readonly metadata under `ROOT/broker/git-safe` on
the existing quota-enforced read-only bind. Only HEAD, index, objects, heads/tags,
shallow and sanitized packed refs are copied through no-follow descriptors;
source configuration, hooks, attributes and object alternates are excluded.
ROOT generates minimal configuration and empty info attributes. Every operation
uses the exact scoped `GIT_DIR`, fixed worktree, cleared system/global config and
attributes, disabled replacement objects, hooks/fsmonitor/credential/diff
overrides and disabled submodule traversal. Tracked attributes may name drivers,
but no executable driver is defined. Metadata has at most4096 entries, depth24,
and min(disk policy,128MiB) logical bytes; directories are ROOT:EngineGID0550 and
single-link files0440. One workspace operation owns a metadata generation, then
removes it. Ordinary `apply` changes worktree bytes; `--cached`/`--index` are
refused because no atomic original-index synchronization is provided.

Views are built in `ROOT/.git-staging`, a ROOT0:0/0700 directory outside the
four worker binds. Every temporary inode is checked against the ROOT inherited
project ID; directories retain quota inheritance and all bytes remain charged
to the same hard limit. Complete0550/0440 trees are atomically renamed into
`broker/git-safe`; retirement first renames a complete tree back to private
staging, then removes it outside the audited namespace. Installation and broker
startup create the fixed ROOT0:0/0600 single-link regular file
`/run/doxa/hardened/SESSION/git-metadata.lock`. ROOT producers take an exclusive
flock only for publication/retirement and initial directory publication; native
quota checks must take the shared lock around their whole quota audit. No partial
0700/0600 tree or recursive deletion is visible to a concurrent shared auditor.
The lock path/inode/owner/mode are validated, wait uses the operation's absolute
deadline, and the fixed lock is never unlinked. Lock ownership grants no readiness.
The lock is empty and its full held/fresh stat identity stays unchanged through
the bounded wait and operation. A workspace build reserves its active count and
exact CID/Engine before copying, then releases the global state lock. Guards can
continue during a slow private copy; lifecycle/staging remains blocked by the
reservation. Before publication and exec, the broker rechecks the exact scope
and live native authority. Native shared audits precede the short exclusive
publication lock, preserving a consistent lock order.

`guard` has no caller fields beyond v/session/op. It runs a fresh native
`provider_exec`/family`none` authority check while persistent provider stdio is
active and returns ready only after current quota/kernel/filter/gateway checks.
Failure independently removes the exact owned CID. No caller-mirror disk sample
or invented usage numbers authorize a new turn.

Gateway startup also pins the protected parent and physical inode/owner/mode/link
identity of `hook.sock`, selected family egress sockets and the private status
socket. Cleanup waits for the captured owned gateway PIDFD to exit, validates
every endpoint, then unlinks only matching leaves through retained parent
descriptors. Unknown sockets, replacements or unsafe metadata require ROOT review;
they never authorize broad path deletion or a stopped receipt.

Native `broker-check`, `stage-check` and gateway stderr are drained concurrently
with stdout and deadlines. At most8192 stderr bytes are retained; overflow is
discarded. Actual native exit, phase/family/CID and limited reason are recorded
in three fixed ROOT600 files under the private runtime directory:
`native-broker-check.json`, `native-stage-check.json`, `native-serve.json`.
These capture native diagnostics only, never provider stdio or provider secret
environment, and are neither client receipts nor admission evidence.

`prove_git_safe_guest.py` executes actual Git as UID1001 with malicious source
clean/smudge/process filter configuration. Unsafe diff/apply reproduce marker
execution; the ROOT-generated metadata must run the reviewed commands without
markers and leave the original index unchanged. This proves execution purity
and readonly metadata, without Engine, quota installation or provider admission.

`provider_snapshot` takes a selected family, fixed worker-home-relative JSONL
path, optional exact thread ID, offset and bounded limit. Codex source prefixes
are `codex/sessions` and `codex/archived_sessions`; Claude uses
`claude/projects/SLUG/SESSION.jsonl`. Actual source must be Engine-owned,
regular, single-link, in the installed project quota and at most 32 MiB. Before
and after reading, live quota and exact source stat facts are checked; source
header thread and workspace cwd must match. Metadata binds current session,
CID, policy, boot, family and actual source device/inode/size/ctime/ns/mtime/ns,
whole-file SHA, relative path and thread. Frame12 carries frozen bytes, frame13
confirms the selected byte count and SHA. Offset windows are at most 8 MiB.

The caller uses `--snapshot-destination PRIVATE_CANONICAL_NEW_FILE`; its helper
writes mode0600 bytes and prints the small metadata JSON to stdout. Root never
opens the destination. `verify_provider_snapshot --expect-file PRIVATE_JSON`
reloads the actual source and requires all original tuple/byte facts to match,
then returns `snapshot-verified`/`matches:true`. The caller's booleans do not grant
admission. A separate directory-discovery operation is reserved; credentials are
never enumerated by the transcript API.

Container creation uses a fixed ROOT-owned name. A lost create reply triggers
independent bounded name reconciliation, exact image/spec/ROOT-label checks and
force-remove/confirmed404. Uncertain creation is quarantined for root review.
Inherited image labels are allowed only from exact pinned image metadata merged
with the two mandatory ROOT labels; inherited metadata grants no authority.
Exited/absent containers remain stoppable. Explicit idle ROOT resume may re-pin a
restarted Engine only after exact read-only config/executable checks, verify the
previous exact CID/image/labels/current quota/kernel scope, remove it and prepare
fresh; active transports never cross an Engine identity change.
