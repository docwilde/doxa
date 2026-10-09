# Disposable egress origin scope proof

Run from the repository root with an absolute, readable Linux kernel image:

```sh
export TMPDIR=/home/docwilde/t
scripts/egress-origin-guest/run.sh /absolute/path/to/vmlinuz /home/docwilde/t
```

The script builds a static example from the production `broker_origin.rs`
module and boots an isolated QEMU initramfs without a network device or disk.
Only the guest mounts cgroup v2 and creates a test cgroup. The example checks
four cases: an in-scope connector is accepted, a host-scope connector is
refused, a dead init pin refuses, and a replacement init needs a new pin.
The script requires all four markers and records binary, source, kernel,
initramfs and serial-log hashes under its task-local evidence directory.

This is a kernel-scope test, not a rootless Docker Engine proof. The guest
supplies its own init PID; it does not authenticate an Engine report, run the
provider adapter, observe per-message writers, or prove that cgroup membership
cannot change after inspection. `docker-hardened` remains unavailable.
