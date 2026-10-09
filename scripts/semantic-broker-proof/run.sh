#!/usr/bin/env bash
# Run the disabled broker identity handshake inside a disposable offline VM.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo 'usage: run.sh STATIC_CODEGRAPH_TEST KERNEL_IMAGE OUTPUT_DIR' >&2
  exit 2
fi
test_binary=$1
kernel_image=$2
output_dir=$3
fixture_dir=$(cd "$(dirname "$0")" && pwd)
[[ -f "$test_binary" && -f "$kernel_image" ]] || {
  echo 'missing static test binary or kernel image' >&2
  exit 2
}
mkdir -p "$output_dir/rootfs"/{bin,etc,dev,proc,sys,run,home/guest}
output_dir=$(cd "$output_dir" && pwd)
rootfs=$output_dir/rootfs
for binary in "$test_binary" /usr/bin/busybox; do
  file "$binary" | grep -Eq 'static-pie linked|statically linked' || {
    echo "guest executable is not statically linked: $binary" >&2
    exit 2
  }
done
cp "$fixture_dir/init" "$rootfs/init"
cp "$fixture_dir/broker_fixture.c" "$output_dir/broker_fixture.c"
cp /usr/bin/busybox "$rootfs/bin/busybox"
ln -sfn busybox "$rootfs/bin/sh"
cp "$test_binary" "$rootfs/codegraph-test"
cat > "$rootfs/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
guest:x:1000:1000:guest:/home/guest:/bin/sh
EOF
cat > "$rootfs/etc/group" <<'EOF'
root:x:0:
guest:x:1000:
EOF
gcc -static -O2 -Wall -Wextra -Werror -o "$rootfs/broker_fixture" "$fixture_dir/broker_fixture.c"
chmod 755 "$rootfs" "$rootfs"/{bin,etc,dev,proc,sys,run,home,home/guest,init,codegraph-test,broker_fixture}
(
  cd "$rootfs"
  find . -print0 | cpio --null -o --format=newc --owner=0:0 2>/dev/null | gzip -1 > "$output_dir/initramfs.cpio.gz"
)
sha256sum "$test_binary" "$kernel_image" "$fixture_dir/broker_fixture.c" > "$output_dir/SHA256SUMS"
timeout 40s qemu-system-x86_64 -machine q35,accel=kvm -cpu host -m 512M -smp 1 \
  -kernel "$kernel_image" -initrd "$output_dir/initramfs.cpio.gz" \
  -append 'console=ttyS0 rdinit=/init panic=1' -display none \
  -serial "file:$output_dir/guest-serial.log" -monitor none -no-reboot -net none
grep -q 'test result: ok. 1 passed; 0 failed' "$output_dir/guest-serial.log"
grep -q 'DOXA_BROKER_TEST_STATUS=0 BROKER_STATUS=0' "$output_dir/guest-serial.log"
echo "identity proof passed; log: $output_dir/guest-serial.log"
