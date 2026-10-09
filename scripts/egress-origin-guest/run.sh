#!/usr/bin/env bash
# Disposable kernel-scope proof. No Docker Engine, network or host cgroup writes.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "usage: TMPDIR=/real/disk $0 /absolute/vmlinuz /absolute/evidence-parent" >&2
  exit 2
fi
kernel=$1
evidence_parent=$2
if [[ ${TMPDIR:-} != /* || ${TMPDIR:-} == /tmp* || $kernel != /* || $evidence_parent != /* ]]; then
  echo "TMPDIR, kernel and evidence parent must be absolute real-disk paths" >&2
  exit 2
fi
if [[ ! -r $kernel || ! -d $evidence_parent ]]; then
  echo "kernel or evidence parent unavailable" >&2
  exit 2
fi
for command in cargo cpio gzip qemu-system-x86_64 rg sha256sum timeout; do
  command -v "$command" >/dev/null || { echo "missing $command" >&2; exit 2; }
done
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
evidence=$(mktemp -d "$evidence_parent/egress-origin-guest.XXXXXXXX")
rootfs=$evidence/rootfs
mkdir -p "$rootfs"/{bin,proc,sys,dev,run}
cp /usr/bin/busybox "$rootfs/bin/busybox"
ln -s busybox "$rootfs/bin/sh"
cp "$repo/scripts/egress-origin-guest/init" "$rootfs/init"
chmod 0755 "$rootfs/init"

TMPDIR=$TMPDIR CARGO_TARGET_DIR="$evidence/target" RUSTFLAGS='-C target-feature=+crt-static' \
  cargo build --locked --target x86_64-unknown-linux-gnu -p doxa-isolation \
  --example egress_origin_guest --release --manifest-path "$repo/Cargo.toml"
cp "$evidence/target/x86_64-unknown-linux-gnu/release/examples/egress_origin_guest" "$rootfs/egress-origin-guest"

(cd "$rootfs" && find . -print0 | cpio --null -o --format=newc -R 0:0 2> "$evidence/cpio.log" | gzip -n > "$evidence/initramfs.cpio.gz")
timeout 120s qemu-system-x86_64 -machine accel=tcg -m 1024 -smp 2 -nographic \
  -no-reboot -net none -kernel "$kernel" -initrd "$evidence/initramfs.cpio.gz" \
  -append 'console=ttyS0 panic=1' > "$evidence/serial.log" 2>&1
if ! rg -q 'DOXA_EGRESS_ORIGIN_GUEST_PASS cases=4 hardened_admission=false' "$evidence/serial.log" \
   || ! rg -q 'DOXA_EGRESS_ORIGIN_VM_STATUS=0' "$evidence/serial.log"; then
  echo "guest proof refused; inspect $evidence/serial.log" >&2
  exit 1
fi
sha256sum "$kernel" "$rootfs/egress-origin-guest" "$evidence/initramfs.cpio.gz" \
  "$evidence/serial.log" "$repo/rust/doxa-isolation/examples/egress_origin_guest.rs" \
  "$repo/scripts/egress-origin-guest/init" "$repo/scripts/egress-origin-guest/run.sh" \
  > "$evidence/SHA256SUMS"
git -C "$repo" rev-parse HEAD > "$evidence/BASE_COMMIT"
echo "guest scope proof passed: $evidence"
