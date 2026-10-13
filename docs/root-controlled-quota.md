# Root-controlled session quotas

The hardened Engine gateway consumes an administrator-installed quota policy
and retained directory descriptors. The older fixture receipts and advisory
`query_advisory_quota` result remain non-authorizing. Neither the provisioning
CLI nor either read-only helper turns a successful snapshot into admission.

This path currently provisions ext4 project quotas. The filesystem administrator
must enable the `project,quota` filesystem features and mount with enforcing
`prjquota` before creating a session. DOXA does not format an existing host
filesystem, enable host-wide quotas, allocate project IDs through worker
requests, or change a live host's services automatically.

An administrator creates one empty session with a distinct project ID and exact
finite hard block limit. The trusted caller and Engine use separate non-root
UIDs; the disposable acceptance host uses caller 1000 and Engine 1001. Every
ancestor and the session root are root owned and not writable by the Engine.
Checkout, home and cache bind roots are root owned, mode `0770`, with a dedicated
Engine data group. The session root and broker are root owned, mode `0750`, in
that group. The data roots have no access or default ACL. A trusted caller may
belong to the data group when needed for workspace inspection; the Engine
control socket stays mode `0600` for the separate Engine UID.

The enforced broker directory permits root-owned `hook.sock`,
`egress-claude.sock` and `egress-codex.sock`, mode `0660` in the Engine group,
plus one optional `git-safe` directory. Each family receives its own immutable
endpoint. The advisory helper retains its older `hook.sock`/`egress.sock`
two-entry snapshot contract and rejects `git-safe`.

`git-safe` provides ROOT-generated immutable Git metadata in the existing
read-only broker mount. Its root is root-owned, Engine-group `0550` or `0750`;
all descendant directories are `0550`, and regular single-link files are
`0440`, with the same root owner and Engine group. No symlink, special entry,
access/default ACL, writable descendant or extra broker entry is accepted.
Descriptor walks check every inode's filesystem, project ID and unique mount
identity against the broker root, plus project inheritance on all directories.
The subtree has at most 4,096 descendants, depth 24, and logical file bytes no
greater than the smaller of the hard project quota and 128 MiB. Directory and
entry identity/timestamp changes during a walk refuse the snapshot; repeated
audits rewind streams to inspect the current contents.

ROOT copies a bounded sanitized Git view and generates its configuration;
worker-owned `.git/config`, filters, includes and hooks are not executable
authority. Git operates on this metadata through the same quota-charged broker
mount, with fixed Git directory/worktree and trusted configuration/environment
overrides. The broker serializes views and publishes a complete ROOT-owned
tree atomically. These ownership/quota checks do not replace review of the
broker's sanitization or its immutable executable and exec-filter checks.

The Engine can create data inside these roots but cannot rename the bind roots
or change their project attributes. New worker-owned descendants remain mutable
unless every worker process inherits the gateway's mandatory security filter.
The root gateway must therefore attest the reviewed image-owned worker binary
and security contract version 1: retain Docker's default filter, deny
`FS_IOC_FSSETXATTR` and native/compat `FS_IOC_SETFLAGS`, remove capabilities,
enable no-new-privileges, make the image filesystem read-only, provide no external
writable bind or Engine socket, and route every provider/workspace execution
through the filtered worker. The ioctl comparison must account for the kernel's
32-bit command truncation. A current inheritance snapshot alone does not meet
that contract. Linux permits an ordinary owner to change these attributes, as
the [kernel file-attribute implementation](https://github.com/torvalds/linux/blob/master/fs/ioctl.c)
and [ext4 implementation](https://github.com/torvalds/linux/blob/master/fs/ext4/ioctl.c)
show.

The quota capability proves neither cgroup containment nor provider identity.
The root Engine gateway must hold and verify those authorities separately.
Worker-writable cgroup movement files or unrestricted direct Engine execution
invalidate hardened admission even when the project hard limit is correct.

## Provision and inspect

Build `doxa-quota-provision` and `doxa-quota-helper` from the reviewed source and
install independently reviewed binaries as root under `/usr/libexec/doxa`.
Create a root-controlled session parent on the already prepared ext4 mount.
The image must be content pinned and the worker hash must describe the exact
reviewed executable inside that image. As root, for example:

```sh
/usr/libexec/doxa/doxa-quota-provision create session-1 \
  /srv/doxa-sessions/session-1 1000 1001 1001 5001 33554432 \
  sha256:ACTUAL_IMAGE_CONTENT_DIGEST ACTUAL_WORKER_SHA256
/usr/libexec/doxa/doxa-quota-provision verify session-1
```

The first command creates the empty tree, sets project inheritance, installs the
actual 32 MiB hard limit, reads back quota accounting and enforcement, captures
directory identities, and installs a root-owned mode `0600` policy at
`/etc/doxa/quota-enforced/session-1.json`. It rejects existing policies/trees,
unsafe ancestors, allocated project IDs, unpinned images, and invalid identities
or limits. An exclusive administrator lock serializes project allocation. The
tree stays inaccessible to the Engine until all bind roots have their project
attributes. A partially failed provision leaves its root-owned tree/project for
explicit administrator inspection; it installs no usable policy.

The policy pins the filesystem device, inode and **unique** mount identity of
the root and all four bind sources. Unique mount IDs require Linux 6.8 or newer;
ordinary `STATX_MNT_ID` values can recycle on the very next remount. The policy
pins the kernel boot ID as well, preventing unique IDs from being reused across
a host reboot. A foreign boot policy refuses before a kernel quota snapshot.
The policy
also pins the separate caller/Engine identities, worker image content digest,
worker binary SHA-256 and security contract version. No successful fixture
receipt, `verified` flag or admission flag is accepted as policy input.

The root gateway calls
`quota_install::enforcement::load_root_enforcement_policy(session_id)` and
`InstalledQuotaPolicy::verify_for_gateway()`. The resulting
`EnforcedQuotaSnapshot` contains the root policy device/inode/SHA-256, exact
kernel quota state and open root/checkout/home/cache/broker descriptors. It has
no admission flag. Call `revalidate()` before every root-controlled Engine
operation and combine the result with the live Engine/worker contract. Policy
replacement, changed bind identities, altered project inheritance, unavailable
quota accounting/enforcement, changed hard limit or mount identity all refuse.

## Requestless installed helper

The root-owned socket and service templates are
`packaging/systemd/doxa-quota-enforced@.socket` and `.service`. The dedicated
`doxa-quota-readers` socket group contains the trusted caller only. The service
loads a fixed policy for its instance and listens at
`/run/doxa/quota-enforced/session-1.sock`. It authenticates the peer UID, accepts
no paths, project IDs, limits or proof data from that peer, and returns a bounded
read-only report. The report explicitly remains non-authorizing. The root
gateway loads the installed policy directly and retains directory capabilities.

These units deliberately share the administrator's mount namespace so their
unique mount IDs match the installed policy. `ProtectSystem`, `ProtectHome`,
`PrivateDevices` or equivalent mount-namespace sandboxing would clone mounts
and make that identity check fail. The enforced helper retains
no-new-privileges, `CAP_SYS_ADMIN` for the quota query and `CAP_DAC_READ_SEARCH`
for opening private worker-owned files solely to read their quota metadata, Unix-only
address families, a mount/module/reboot/raw-I/O/swap syscall deny filter, and
other restrictions that preserve the mount namespace. Its administrator policy
and executable are root controlled; it exposes no request parser capable of
selecting another resource.

## Restart, remount and proof

An Engine restart leaves current project quota and pinned source identities
intact. A source remount changes the unique mount identity, so old policies and
open gateway/helper leases refuse. With the Engine/gateway stopped, an
administrator may run `refresh-after-remount session-1`. That command permits
only mount identity rebinding: all directory devices/inodes, project attributes,
hard limit and root-controlled permissions must remain exact. It atomically
repins the current kernel boot ID after those exact checks and
replaces the root policy. Old leases refuse the new policy inode even when the
policy's other content is identical. Stop the old activated helper service and
reconnect through its socket to load the replacement; restart the gateway
explicitly. The helper never refreshes policy itself.

`scripts/prove_root_quota_policy_guest.py` is restricted to an explicitly
acknowledged disposable Ubuntu 26.04 KVM guest. It uses a private 1 GiB ext4
loopback filesystem under `/root/isolation-proof`, leaving any shared guest
data disk untouched. It checks real root provisioning, UID separation, duplicate
project refusal, mutable-root and directory-identity refusal, ordinary owner
attribute changes on a descendant, stale mount refusal and administrator
rebinding. `scripts/prove_root_quota_helper_guest.py` verifies installed
socket activation, root peer identity, unauthorized worker denial, request-body
non-selection, tampered-policy refusal and policy-lease invalidation/reactivation.
Those kernel/installation proofs use test-only image pins and authorize no
worker profile.

`scripts/prove_filtered_quota_guest.py` runs the bounded container write portion
with `scripts/quota_filtered_fixture_provider.py` installed as an image-owned,
credential-free `/usr/local/bin/claude` workload. The container's init uses
`hold-hardened`, and every workload invocation uses `exec-hardened`. The root
orchestrator pins and inspects the image, worker binary, four exact bind sources,
read-only root filesystem and network-none boundary. It proves attribute-mutation
denial on an actual new UID-1001-owned descendant, solo and aggregate `EDQUOT`,
container/Engine restart and source remount persistence, broker write refusal,
and final container/file cleanup. On a shared guest, its root-owned restart
barrier pauses after the initial proof until the coordinator supplies this exact
container ID in a mode-0600 file. That lifecycle barrier does not affect quota
admission and cannot be created by the Engine UID. The workload is not a live
provider and its reports never assert admission.

Before admission, the exact filtered worker image still needs positive bounded
writes through checkout/home/cache ending in `EDQUOT`, aggregate cross-bind
accounting, persistence after container/Engine restart and source remount, a
read-only broker write refusal, and project/inheritance mutation attempts on
new worker-owned descendants. The root gateway's exact Engine, caller and
filtered-exec authority must be established independently. Record both positive
and negative controls and complete container/file/service cleanup. Fixture
JSON is evidence about that disposable run; it is never a transferable token.
