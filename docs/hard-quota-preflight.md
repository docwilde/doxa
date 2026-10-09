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
  --max-write-mib 128 --acknowledge-fixture-writes
```

The probe checks the read-only prerequisites again, refuses nonempty bind
sources, uses an empty Docker CLI config, and starts a credential-free,
network-none container with only the three fixture binds. It writes and
`fsync`s at most 128 MiB per bind, deleting each probe file before the next.
Only positive bounded writes ending in `EDQUOT` on **all three** binds produce a
fixture proof. `ENOSPC`, no error before the cap, an unexpected receipt, a
non-rootless Engine, and low host free space all refuse the proof. It removes
only its random test container. The JSON still sets
`admissible_as_hard_quota` to `false`: it is evidence for this fixture at this
moment, not runtime admission or proof across restart/remount. No quota is
configured or changed by the probe. The host's ordinary Docker context and
credential helpers are not used.

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
