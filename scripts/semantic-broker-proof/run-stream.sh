#!/usr/bin/env bash
# Run a six-case stream-origin fixture in an offline disposable QEMU guest.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo 'usage: run-stream.sh STATIC_CODEGRAPH_TEST KERNEL_IMAGE OUTPUT_DIR' >&2
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
cp "$fixture_dir/stream-init" "$rootfs/init"
cp "$fixture_dir/stream_fixture.c" "$output_dir/stream_fixture.c"
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
gcc -static -O2 -Wall -Wextra -Werror -o "$rootfs/stream_fixture" "$fixture_dir/stream_fixture.c"
chmod 755 "$rootfs" "$rootfs"/{bin,etc,dev,proc,sys,run,home,home/guest,init,codegraph-test,stream_fixture}
(
  cd "$rootfs"
  find . -print0 | cpio --null -o --format=newc --owner=0:0 2>/dev/null | gzip -1 > "$output_dir/initramfs.cpio.gz"
)
sha256sum "$test_binary" "$kernel_image" "$fixture_dir/stream_fixture.c" \
  "$fixture_dir/stream-init" "$fixture_dir/run-stream.sh" \
  "$rootfs/stream_fixture" "$output_dir/initramfs.cpio.gz" > "$output_dir/SHA256SUMS"
timeout 85s qemu-system-x86_64 -machine q35,accel=kvm -cpu host -m 512M -smp 1 \
  -kernel "$kernel_image" -initrd "$output_dir/initramfs.cpio.gz" \
  -append 'console=ttyS0 rdinit=/init panic=1' -display none \
  -serial "file:$output_dir/guest-serial.log" -monitor none -no-reboot -net none
[[ $(grep -c 'test result: ok. 1 passed; 0 failed' "$output_dir/guest-serial.log") -eq 6 ]]
for mode in root handoff mid_handoff root_switch cid_swap extra; do
  grep -q "DOXA_STREAM_MODE=$mode TEST_STATUS=0 BROKER_STATUS=0" "$output_dir/guest-serial.log"
done
grep -q 'DOXA_STREAM_ALL_STATUS=0' "$output_dir/guest-serial.log"
echo "stream sender and packet continuity passed; log: $output_dir/guest-serial.log"
