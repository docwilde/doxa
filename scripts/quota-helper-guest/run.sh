#!/usr/bin/env bash
# Disposable, credential-free QEMU acceptance for the uninstalled quota helper.
# All filesystem and quota configuration happens on a task-local virtual disk.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: TMPDIR=/real/disk $0 /absolute/vmlinuz-VERSION /absolute/evidence-parent" >&2
  exit 2
fi
kernel=$1
evidence_parent=$2
if [[ ${TMPDIR:-} != /* || ${TMPDIR:-} == /tmp* || $kernel != /* || $evidence_parent != /* ]]; then
  echo "TMPDIR, kernel, and evidence parent must be absolute real-disk paths" >&2
  exit 2
fi
if [[ ! -r $kernel || ! -d $evidence_parent ]]; then
  echo "kernel or evidence parent unavailable" >&2
  exit 2
fi
for command in cargo gcc cpio gzip mkfs.ext4 qemu-system-x86_64 rg sha256sum timeout zstd; do
  command -v "$command" >/dev/null || { echo "missing $command" >&2; exit 2; }
done

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
kernel_release=${kernel##*/}
kernel_release=${kernel_release#vmlinuz-}
module_root=/lib/modules/$kernel_release/kernel
for module in fs/quota/quota_tree.ko.zst fs/quota/quota_v2.ko.zst drivers/char/hw_random/virtio-rng.ko.zst; do
  [[ -r $module_root/$module ]] || { echo "missing $module_root/$module" >&2; exit 2; }
done

evidence=$(mktemp -d "$evidence_parent/quota-helper-guest.XXXXXXXX")
rootfs=$evidence/rootfs
mkdir -p "$rootfs"/{bin,proc,sys,dev,etc,run,quota,alias,lib/modules}
chmod 0755 "$rootfs" "$rootfs"/{bin,proc,sys,dev,etc,run,quota,alias,lib,lib/modules}
cp /usr/bin/busybox "$rootfs/bin/busybox"
ln -s busybox "$rootfs/bin/sh"
cp "$repo/scripts/quota-helper-guest/init" "$rootfs/init"
chmod 0755 "$rootfs/init"

TMPDIR=$TMPDIR CARGO_TARGET_DIR="$evidence/target" RUSTFLAGS='-C target-feature=+crt-static' \
  cargo build --locked --target x86_64-unknown-linux-gnu -p doxa-isolation \
  --bin doxa-quota-helper --example quota_helper_client_read --release --manifest-path "$repo/Cargo.toml"
cp "$evidence/target/x86_64-unknown-linux-gnu/release/doxa-quota-helper" "$rootfs/doxa-quota-helper"
cp "$evidence/target/x86_64-unknown-linux-gnu/release/examples/quota_helper_client_read" "$rootfs/quota_helper_client_read"
gcc -static -O2 -Wall -Wextra -o "$rootfs/guest_harness" "$repo/scripts/quota-helper-guest/harness.c"
zstd -dc "$module_root/fs/quota/quota_tree.ko.zst" > "$rootfs/lib/modules/quota_tree.ko"
zstd -dc "$module_root/fs/quota/quota_v2.ko.zst" > "$rootfs/lib/modules/quota_v2.ko"
zstd -dc "$module_root/drivers/char/hw_random/virtio-rng.ko.zst" > "$rootfs/lib/modules/virtio-rng.ko"

(cd "$rootfs" && find . -print0 | cpio --null -o --format=newc -R 0:0 2> "$evidence/cpio.log" | gzip -n > "$evidence/initramfs.cpio.gz")
truncate -s 128M "$evidence/quota.raw"
TMPDIR=$TMPDIR mkfs.ext4 -F -O project,quota -E lazy_itable_init=0,lazy_journal_init=0 \
  "$evidence/quota.raw" > "$evidence/mkfs.log" 2>&1
timeout 120s qemu-system-x86_64 -machine accel=tcg -m 1024 -smp 2 -nographic \
  -no-reboot -net none -object rng-random,filename=/dev/urandom,id=rng0 \
  -device virtio-rng-pci,rng=rng0 -kernel "$kernel" \
  -initrd "$evidence/initramfs.cpio.gz" -append 'console=ttyS0 panic=1' \
  -drive "file=$evidence/quota.raw,format=raw,if=virtio" \
  > "$evidence/serial.log" 2>&1
if ! rg -q 'DOXA_QUOTA_HELPER_GUEST_PASS cases=25 admission=false' "$evidence/serial.log" \
   || ! rg -q 'DOXA_QUOTA_VM_STATUS=0' "$evidence/serial.log"; then
  echo "guest proof refused; inspect $evidence/serial.log" >&2
  exit 1
fi
sha256sum "$kernel" "$rootfs/doxa-quota-helper" "$rootfs/quota_helper_client_read" "$rootfs/guest_harness" \
  "$evidence/initramfs.cpio.gz" "$evidence/quota.raw" "$evidence/serial.log" \
  > "$evidence/SHA256SUMS"
sha256sum "$repo/rust/doxa-isolation/src/quota_helper.rs" \
  "$repo/scripts/quota-helper-guest/harness.c" "$repo/scripts/quota-helper-guest/init" \
  "$repo/scripts/quota-helper-guest/run.sh" >> "$evidence/SHA256SUMS"
git -C "$repo" rev-parse HEAD > "$evidence/BASE_COMMIT"
echo "guest proof passed: $evidence"
