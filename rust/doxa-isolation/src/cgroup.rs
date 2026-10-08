//! Verify the kernel limits visible to the session worker, rather than only
//! trusting Docker's requested HostConfig values.
use std::{fs::File, io::{self, Read}, path::Path};

use crate::error;

fn read_bounded(path: &Path) -> io::Result<String> {
    let mut bytes = Vec::new();
    File::open(path)?.take(129).read_to_end(&mut bytes)?;
    if bytes.len() > 128 { return Err(error("cgroup controller value is too large")); }
    String::from_utf8(bytes).map_err(|_| error("cgroup controller value is not UTF-8"))
}

fn finite_limit(value: &str, name: &str) -> io::Result<u64> {
    let value = value.trim();
    if value.is_empty() || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error(format!("{name} has no finite cgroup v2 limit")));
    }
    value.parse().map_err(|_| error(format!("{name} exceeds a u64 cgroup limit")))
}

fn verify_values(memory: &str, swap: &str, cpu: &str, pids: &str, expected_memory: u64, expected_cpus: f64, expected_pids: u32) -> io::Result<()> {
    if !expected_cpus.is_finite() || expected_cpus <= 0.0 { return Err(error("invalid expected CPU ceiling")); }
    if finite_limit(memory, "memory.max")? > expected_memory {
        return Err(error("effective cgroup memory.max exceeds session policy"));
    }
    if finite_limit(swap, "memory.swap.max")? != 0 {
        return Err(error("effective cgroup permits swap despite session policy"));
    }
    if finite_limit(pids, "pids.max")? > u64::from(expected_pids) {
        return Err(error("effective cgroup pids.max exceeds session policy"));
    }
    let fields = cpu.split_whitespace().collect::<Vec<_>>();
    if fields.len() != 2 { return Err(error("cpu.max has no finite quota and period")); }
    let quota = finite_limit(fields[0], "cpu.max quota")?;
    let period = finite_limit(fields[1], "cpu.max period")?;
    if quota == 0 || period == 0 { return Err(error("cpu.max has an invalid quota or period")); }
    // Docker's --cpus conversion can round a quota up by one microsecond.
    let expected_nanos = (expected_cpus * 1_000_000_000.0) as u64;
    if u128::from(quota - 1) * 1_000_000_000 > u128::from(expected_nanos) * u128::from(period) {
        return Err(error("effective cgroup cpu.max exceeds session policy"));
    }
    Ok(())
}

/// Called from inside the image-owned worker after Docker exec has entered
/// the container's private cgroup namespace. Missing or unlimited controllers
/// refuse admission, even if Docker inspect still reports the requested flags.
#[cfg(target_os = "linux")]
pub fn verify_limits(memory: u64, cpus: f64, pids: u32) -> io::Result<()> {
    let root = Path::new("/sys/fs/cgroup");
    let mut stats = std::mem::MaybeUninit::<libc::statfs>::uninit();
    let path = c"/sys/fs/cgroup";
    if unsafe { libc::statfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 { return Err(io::Error::last_os_error()); }
    if unsafe { stats.assume_init() }.f_type != 0x6367_7270 {
        return Err(error("worker has no cgroup v2 filesystem"));
    }
    if read_bounded(Path::new("/proc/self/cgroup"))?.trim() != "0::/" {
        return Err(error("worker is not rooted in its private cgroup namespace"));
    }
    verify_values(
        &read_bounded(&root.join("memory.max"))?,
        &read_bounded(&root.join("memory.swap.max"))?,
        &read_bounded(&root.join("cpu.max"))?,
        &read_bounded(&root.join("pids.max"))?,
        memory, cpus, pids,
    )
}

#[cfg(not(target_os = "linux"))]
pub fn verify_limits(_memory: u64, _cpus: f64, _pids: u32) -> io::Result<()> {
    Err(error("Docker cgroup enforcement requires Linux"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_kernel_limits_are_admitted() {
        verify_values("536870912\n", "0\n", "150000 100000\n", "128\n", 536870912, 1.5, 128).unwrap();
        verify_values("268435456", "0", "100000 100000", "64", 536870912, 1.5, 128).unwrap();
        verify_values("536870912", "0", "150001 100000", "128", 536870912, 1.5, 128).unwrap();
    }

    #[test]
    fn missing_unlimited_or_weakened_kernel_limits_are_refused() {
        for (memory, swap, cpu, pids) in [
            ("max", "0", "150000 100000", "128"),
            ("536870913", "0", "150000 100000", "128"),
            ("536870912", "max", "150000 100000", "128"),
            ("536870912", "1", "150000 100000", "128"),
            ("536870912", "0", "max 100000", "128"),
            ("536870912", "0", "150002 100000", "128"),
            ("536870912", "0", "150000 100000", "max"),
            ("536870912", "0", "150000 100000", "129"),
            ("536870912", "0", "150000 0", "128"),
            ("536870912", "0", "150000", "128"),
        ] {
            assert!(verify_values(memory, swap, cpu, pids, 536870912, 1.5, 128).is_err(), "{memory} {swap} {cpu} {pids}");
        }
    }
}
