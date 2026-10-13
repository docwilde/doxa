# Actual ROOT authority and origin proof

Run6 completed with exit 0 on the authorized disposable guest on 2026-10-13.
The exact JSON output is `run6-report.json`, SHA256
`71a82736e3e85893da589eca62607b8f9711680a7c8edbccee3c00133554649f`.

The exact executed source is `scripts/prove_root_authority_guest.py` at this
commit, SHA256
`a382195fa76e34b069c970a602f3389329f24c4de806d2ac593f9aacf9399117`.
The guest file had the same SHA. This source includes current worker embedding,
TTY exec handling, delegated/post-handshake writers, SCM_RIGHTS host PTY probing,
and full actual mountinfo capture. No Docker CLI or shell performs Engine work.

## Exact scope and pins

| Object | Value |
| --- | --- |
| Parent production source | `05152e6b` |
| Authority module source | `4b022adc` (reader `46874b04`) |
| Native gateway SHA256 | `7a634a8b36380fbfab9b1ffb818bf485e1fa58789ae988b5f85e994fdc7b43c3` |
| Worker SHA256 | `06fd63d262a84c6e49df2e33bb10d27247ab943e787247279ce1a2bff03908cb` |
| ROOT broker source | `f26a69b7` |
| ROOT broker SHA256 | `34f2497eba2ba6ac5d40a2740d2996eed35908000a2393c45b520dfe97c8fe17` |
| Fixture provider SHA256 | `6ad420a142938942c5609ea81709f229a1d91d37d9ab24e0bd14a3c58b1f324d` |
| Immutable base image | `sha256:5eb3f63be16d9bd9d0794e52feb02534ca6c1d581d535c8d3adb3df012aa2eaa` |
| Built fixture image | `sha256:b0ed50bef3b08af8ff10cbe4895a62e635294697694a67ad45de8185bef2aaa7` |
| ROOT policy SHA256 | `7b93823c4e22b19f5ac1042a1dfc0a3ffb2cffff4936342f900f39bb360a7c9b` |
| Session / project / hard quota | `root-authority-20261013-r6` / `5009` / `67108864` bytes |
| Live Engine / socket inode | PID `27821`, dev `56`, inode `688`, UID/GID `1001:1001`, mode `0600` |
| Container / retained init | `606d074eca1fe61dc0f71d379ef5aea6fa39630cac4774cfda890609e0f68825` / PID `53419` |
| Sibling negative CID | `ca72f3d8d006ea09eec85747e78d4e1432ab9489763004877463fb53ff696dad` |

The ROOT policy pins the live Engine executable and ROOT configuration. The
native diagnostic inherits the exact Engine pidfd, independently verifies its
Unix peer, and reads actual process/filesystem/cgroup facts. Engine inspection,
quota receipts and helper JSON do not substitute for those checks.

## Actual results

The native kernel check passed before and after the restriction probe. Inside
container and private PTY frames passed with exact echoed sender payloads. The
post-handshake original writer's first frame passed before its child's frame
was refused.

| Negative | Actual refusal |
| --- | --- |
| Delegated descriptor writer | `guarded stream writer differs from connector` |
| Child writer after accepted first frame | `guarded stream writer differs from connector` |
| Delegated ROOT host PTY | `worker PTY descriptor is outside its private devpts instance` |
| Host UID1000 | Kernel socket connect `EACCES` (`13`) |
| Host UID1001 | `connector is outside the pinned container namespace/cgroup` |
| Sibling container | `connector is outside the pinned container namespace/cgroup` |

The worker observed cgroup control write-open `EROFS` (`30`), `setns`/`unshare`
and quota-changing `FSSETXATTR` `EPERM` (`1`). Project `5009` and inheritance
remained unchanged. Actual status was capabilities zero, NNP1, Seccomp2 and two
filters. The native gate also independently drops to client UID1000/groups to
check Engine ancestor cgroup controls.

Private PTY stdout/stderr were `/dev/pts/0`, device `70`, inode `3`, rdev `34816`.
The deliberately delegated host PTY had the same pathname/inode/rdev but device
`27`; exact private devpts identity therefore distinguishes them. The fixture
received a reset during its second frame, so its exception omitted its final
metadata JSON. The native inventory refusal and ROOT source FD identity are
recorded. No `/proc/PID/stat` `tty_nr` or controlling-terminal claim is made.

Both CIDs were deleted and exact Engine GET returned404. Both captured actual
cgroups and every proof socket were confirmed absent. ROOT quota/data/policy
artifacts were preserved. Engine PID/socket/configuration were unchanged.

## Reproduction

This was the exact guest command, with the above immutable binaries installed
in the authorized VM:

```sh
python3 /root/isolation-proof/root-authority-code/prove_root_authority_guest.py \
  --broker /root/isolation-proof/root-authority-code/root_broker.py \
  --gateway /usr/libexec/doxa/doxa-hardened-gateway-isolation-proof \
  --quota-provision /root/isolation-proof/quota-bin/doxa-quota-provision-family \
  --worker-file /root/isolation-proof/doxa-isolation-worker-current \
  --worker-sha256 06fd63d262a84c6e49df2e33bb10d27247ab943e787247279ce1a2bff03908cb \
  --session-id root-authority-20261013-r6 --project-id 5009 \
  --output /root/isolation-proof/root-authority-run6 --trace-native
```

A replay must use a fresh approved session/project/output directory and the
same pinned versions; the historical scope is deliberately not adopted. Later
gateway versions also require the ROOT-owned session `git-metadata.lock` before
diagnostics. That lock was introduced after this measured source and is not a
fact claimed by run6.

## Limits

This is a credential-free kernel/fixture-origin proof. There are no paid or live
provider calls, full gateway/Hook/Runtime admission, or caller-supplied ready
flags. ROOT-assisted outside-send/move-back was not performed. Immutable
migration restrictions plus per-segment SCM sender checks were measured; a
freeze or sequential snapshot alone is not a send-time cgroup proof.

The namespace reader forks from Rust and may inherit an allocator/runtime lock
in a multithreaded gateway. Its total deadline, pidfd kill and bounded WNOHANG
reap fail closed; they do not establish that such an inherited lock is absent.
Full composed admission remains coordinator-owned.
