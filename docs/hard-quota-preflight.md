# Hard disk quota preflight (operator fixture)

DOXA's Docker disk ceiling is a **monitored turn gate**. A worker can write
between samples. The isolation chip therefore reports no hard filesystem quota.
This read-only Linux preflight helps an operator identify missing prerequisites
for an administrator-managed project quota. It neither configures quotas nor
enables a hard-quota session profile.

## Run on a private fixture

Run on the Docker daemon host in its mount namespace. Create an owner-only
directory on real disk with empty `checkout`, `home` and `cache` children. Use
a task-local path, never a live DOXA session root or provider home. From the
repository root:

```sh
export TMPDIR=/path/on/real/disk
fixture=$(mktemp -d "$TMPDIR/doxa-quota-fixture.XXXXXXXX")
install -d -m 0700 "$fixture/checkout" "$fixture/home" "$fixture/cache"
python3 scripts/check_docker_quota_preflight.py "$fixture"
```

The command reads `/proc/self/mountinfo` and Linux `FS_IOC_FSGETXATTR`; it
opens directories without following symlinks and writes nothing. JSON always
sets `hard_enforcement_verified` and `admissible_as_hard_quota` to `false`.
Exit **2** means a prerequisite failed or could not be inspected. Exit **3**
means the root and three bind sources are an unverified capability candidate;
it is still deliberately nonzero. Remove the empty fixture after review.

The candidate check requires private owner-owned directories, no nested host
mount, one mount and filesystem for all sources, explicit `prjquota`/`pquota`
on XFS or ext4, and one nonzero project ID with project inheritance on the
root and each bind source. It also walks existing descendants through open
directory descriptors: every file and directory must carry that project ID,
and every directory must inherit it. Symlinks, special entries, inaccessible
metadata, more than 4,096 descendants, or more than 64 directory levels
refuse the candidate. The JSON reports `descendants_checked` on a completed
walk. The tool reads metadata only, never file contents.

These are necessary hints, not proof of an active hard block limit. A file
or mount can change after this snapshot, and the test does not exercise Docker.

## Evidence required before production support

An administrator must provision a unique per-session project ID, enable
accounting **and enforcement** on the backing filesystem, set a nonzero hard
block limit, and show the effective limit through filesystem quota tooling.
For XFS, record the `prjquota` mount and a numeric project report from
`xfs_quota`; consult the filesystem administrator for ext4 tooling. Run this
preflight against the exact private tree being assessed and repeat after
provisioning or changing its contents.

Then use a separate, credential-free fixture with a deliberately small hard
limit on a task-local rootless Engine.
Bind its checkout, home and cache exactly as a session worker would. Write
from inside the container through each path, and verify aggregate writes
receive `EDQUOT` at the configured project limit while unrelated host space
remains free. Verify the same result after stop/restart and source remounts.
Do not use a live provider, DOXA store, or host quota changes for this probe.

An opt-in probe now performs the bounded container write portion. After the
administrator has set a **small enforced** project quota on an otherwise empty
owner-private fixture under a real-disk `TMPDIR`, use a pinned reviewed image
with Python 3 and an explicit task-local rootless Engine socket:

```sh
export TMPDIR=/path/on/real/disk
python3 scripts/probe_docker_hard_quota.py "$fixture" \
  --docker-host "unix:///run/user/$(id -u)/doxa-test-docker.sock" \
  --image "sha256:PINNED_CONTENT_ID" \
  --max-write-mib 128 --aggregate-restart --acknowledge-fixture-writes
```

The probe checks the read-only prerequisites again, refuses nonempty bind
sources, uses an empty Docker CLI config, and starts a credential-free,
network-none container with only the three fixture binds. Each solo write
attempt writes and `fsync`s at most 128 MiB, deleting its probe file before
the next bind is tested.
Only positive bounded writes ending in `EDQUOT` on **all three** binds produce
the initial fixture proof. With `--aggregate-restart`, the probe then creates
one inspected `network=none` container, keeps positive writes in checkout and
home, and fills cache until `EDQUOT`. Cache must hit its limit earlier than its
solo baseline by approximately the two retained writes. The probe stops and
restarts that same container, checks all three file sizes, and requires another
bounded `EDQUOT` when appending to cache. It removes its random container and
marker files after a successful stop; if removal is uncertain, it leaves the
markers for operator review. It does **not** restart the Docker Engine or
remount the filesystem.

`ENOSPC`, no error before the cap, an unexpected receipt, a changed mount,
a non-rootless Engine, or low host free space refuses the proof. The 512 MiB
host-space floor is read from the enclosing mount root because XFS can report
the small project limit as `statvfs` free space at the fixture path. The JSON
reports `aggregate_restart_verified_for_fixture` separately and always sets
`admissible_as_hard_quota` to `false`. These are observations for this disposable
fixture, not runtime admission or a broker-path/remount proof. No quota is
configured or changed by the probe. The host's ordinary Docker context and
credential helpers are not used.

On a disposable Ubuntu 26.04 guest (kernel 7.0.0-34), rootless Docker 29.1.3
and an ext4 `prjquota` fixture with a 32 MiB project hard limit, all three
binds returned `EDQUOT` and the aggregate limit persisted after restarting
the same container. The receipt still reports `admissible_as_hard_quota=false`.
An otherwise equivalent XFS fixture returned `ENOSPC` at the limit and was
correctly refused. That first run did not restart the Engine, remount a source,
or test the broker path.

A second fresh disposable guest tested the production-shaped four binds:
checkout, home and cache writable; broker read-only. On ext4 with project ID
1002 and a 32 MiB hard limit, the inspected rootless `network=none` container
kept its identity while the Engine restarted (PID 3736 to 5893) and the
filesystem was unmounted and remounted (mount ID 382 to 366). Checkout and
home each retained 8,388,608 bytes. Cache received `EDQUOT` (errno 122) after
16,711,680 bytes before the restart and immediately on append afterward.
The broker socket replied on both sides of the transition; attempted worker
creation in its read-only bind returned `EROFS` (errno 30). A separate XFS
four-bind control with project ID 1001 returned `ENOSPC` (errno 28) after
33,554,432 bytes, not `EDQUOT`. The disposable container and markers were
removed. These are exact-fixture observations, not a production session or
admission token. The observed strict `EDQUOT` behavior occurred on ext4;
the XFS fixture does not satisfy that gate.

The Rust hardened-admission seam reads a bounded receipt and checks the saved
session profile and exact tree. Its read-only Linux XFS/ext4 verifier can inspect
the exact session root and four bind-source directory descriptors, then walk
up to 4,096 existing entries and 64 directory levels within checkout, home
and cache. It checks project ID, directory inheritance, filesystem and mount
identity, rejects symlinks and special entries, and compares entry identities
again after inspection. On Linux it classifies each entry without following a
link, pins it with `O_PATH`, then reopens the pinned regular file or directory
through procfs for quota metadata. Missing procfs refuses the snapshot. The
broker directory must share the private owner, project ID, inheritance and
mount identity. Its bounded direct-entry audit allows only owner-private
`hook.sock` and `egress.sock` Unix sockets on that mount; any other entry or
replacement during inspection refuses the snapshot. The verifier also reads
the effective project hard block limit with accounting and enforcement enabled.
It requires an explicit exact limit; the fixture's maximum write size is
**not** that limit.
Unsupported filesystems, unavailable `quotactl_fd`, and any mismatch refuse
verification. A rootless owner can receive `EPERM` or `EACCES` from the
project-limit query: XFS/ext4 `Q_XGETQUOTA` requires `CAP_SYS_ADMIN` for this
project ID. The verifier reports that denial and refuses. An opt-in
`doxa-quota-helper` now supplies this read-only query. It requires an
administrator-installed, root-owned mode `0600` JSON policy and one systemd
socket-activation descriptor. Its policy fixes a session root, project ID,
exact hard limit, filesystem device, mount ID and inode for the root and all
four bind sources. It opens and rechecks those descriptors, authenticates the
peer through `SO_PEERCRED`, and requires a dedicated caller UID distinct from
the session tree owner. The peer supplies no request body, path, project ID or
expected limit. The result always says `admissible_as_hard_quota=false`. The
helper does not configure quotas, call Docker, or enable `docker-hardened`.

The service is inert unless an administrator explicitly installs a policy and
socket unit. The policy path and socket path must be absolute; every policy
ancestor must be root-owned and not group/other writable. The policy itself
must be a regular single-link root-owned file with exact mode `0600` and at
most 8 KiB. An illustrative policy shape is:

```json
{
  "version": 1,
  "session_id": "SESSION_ID",
  "root": "/private/isolation/SESSION_ID",
  "socket_path": "/run/doxa/quota/SESSION_ID.sock",
  "caller_uid": 2001,
  "owner_uid": 2002,
  "project_id": 1002,
  "hard_limit_bytes": 33554432,
  "bindings": {
    "root": {"device": 1, "inode": 2, "mount_id": 3},
    "checkout": {"device": 1, "inode": 4, "mount_id": 3},
    "home": {"device": 1, "inode": 5, "mount_id": 3},
    "cache": {"device": 1, "inode": 6, "mount_id": 3},
    "broker": {"device": 1, "inode": 7, "mount_id": 3}
  }
}
```

Those numbers are placeholders; an administrator must capture the live exact
identities and provision quota accounting, enforcement and the hard limit
separately. The activation socket must be root-owned, bound to the policy
path, and accessible only to the dedicated caller. DOXA's current host
controller and rootless worker commonly share a host UID, so this deliberately
refuses their existing arrangement; a separated service identity and reviewed
runtime integration are still needed. Root privilege or `CAP_SYS_ADMIN` for
the project quota syscall must stay with the helper. No live host service is
installed by the repository.
The descendant walk covers the three data bind sources; it does not establish
an immutable tree. The broker audit may report zero sockets, and its socket
names and inode snapshots do not bind an entry to the live host listener or
authenticate a connecting peer. Admission needs the expected live endpoint
identity and a kernel-stable peer-to-container check for each connection.
This is a point-in-time snapshot, not an EDQUOT/restart proof. The
receipt is not an owner-controlled policy, and the admission seam still
refuses even a
hand-edited `admissible_as_hard_quota=true`. Selecting `docker-hardened` remains
unavailable.

### Disposable read-only kernel query

The fixture-only `quota_fixture_read` example uses the existing XFS/ext4
descriptor verifier. It checks the root descriptor against the exact visible
root, opens all four bind sources relative to it, walks existing data-bind
entries, and compares the project ID, accounting/enforcement state and exact
effective hard limit. It always prints `admissible_as_hard_quota=false`. Build
it with `cargo build --locked -p doxa-isolation --example quota_fixture_read`;
run the resulting binary and `scripts/test_quota_fixture_guest.py` only in a
disposable Linux guest with an operator-provisioned four-bind quota fixture:

```sh
sudo python3 scripts/test_quota_fixture_guest.py \
  /absolute/path/to/quota_fixture_read /absolute/fixture/root \
  1002 33554432 1000
```

The script makes only read-only calls. It requires the exact query to succeed
and a substituted root FD, wrong directory, wrong project ID and wrong hard
limit to refuse. On a fresh QEMU Ubuntu 26.04.1 guest with ext4 `prjquota`,
project 1002 and a 32 MiB hard limit, the exact query returned the limit,
project ID and mount identity; all four adversarial inputs refused. A guest
rootless query was denied. Passing a descriptor for a bind-mounted alias of
the same root inode refused on mount identity; changing the broker directory
to project 1003 and disabling project enforcement also refused. Restoring the
project ID and enforcement restored the positive result. The signed-image and
command receipts are in the task-local guest evidence. This exercises ext4's
descriptor-bound `Q_XGETQSTATV` and `Q_XGETQUOTA` branch with a privileged
read-only query, including an enforcement-off negative control.

This example is **not** a deployable privileged helper. Its CLI accepts the
expected project, limit and owner UID from its caller, so it cannot establish
an owner-controlled policy. It has no authenticated helper transport or
privilege boundary. A production helper must bind those values to trusted
owner policy and the exact session tree, constrain its privilege and lifetime,
and return a non-forgeable result to the admission path. Runtime EDQUOT and
broker-origin proofs remain separate requirements. `docker-hardened` stays
closed.

The Codex hook broker checks the Unix peer owner UID and a bounded
`PreCompact` frame. A local same-UID process with the session capability can
still satisfy both checks; that negative fixture is in the isolation tests.
Production hardened admission requires kernel-stable peer-to-container origin
evidence for each accepted connection, with host and sibling processes denied
through worker, daemon and Engine restarts. This has not been proved.

For the focused refusal-path tests:

```sh
TMPDIR=/path/on/real/disk python3 -m unittest discover -s scripts -p 'test_docker_*quota*.py'
```
Only after that evidence and a reviewed runtime admission path may DOXA label
any profile as hard-quota enforced. Docker writable-layer limits alone do not
bound these bind mounts.

The focused fixtures run with:

```sh
TMPDIR=/path/on/real/disk python3 -m unittest discover -s scripts -p test_docker_quota_preflight.py
```
