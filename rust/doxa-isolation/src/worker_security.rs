//! Mandatory image-owned restrictions layered onto Docker's default seccomp.
//! Quota inheritance on owner-created descendants is mutable without this
//! filter. Every hardened init/exec must enter here before starting threads or
//! provider code; no environment flag can remove a kernel-installed filter.
use crate::error;
use std::io;

pub const CONTRACT: &str = "doxa-hardened-worker-v1";

#[cfg(all(target_os = "linux", any(target_arch = "x86_64", target_arch = "aarch64")))]
pub fn install() -> io::Result<()> {
    use libc::{sock_filter, sock_fprog};
    const LOAD: u16 = 0x20;
    const JEQ: u16 = 0x15;
    const JUMP: u16 = 0x05;
    const AND: u16 = 0x54;
    const RET: u16 = 0x06;
    const ALLOW: u32 = 0x7fff0000;
    const KILL: u32 = 0x80000000;
    const DENY: u32 = 0x00050000 | libc::EPERM as u32;
    const UNSUPPORTED: u32 = 0x00050000 | libc::ENOSYS as u32;
    fn instruction(code: u16, k: u32) -> sock_filter { sock_filter { code, jt:0, jf:0, k } }
    fn equal(k: u32, otherwise: u8) -> sock_filter { sock_filter { code:JEQ, jt:0, jf:otherwise, k } }
    fn abi(ioctl: u32, clone: u32, forbidden: &[u32]) -> Vec<sock_filter> {
        let mut out = vec![instruction(LOAD, 0), equal(ioctl, 1), instruction(JUMP, 0)];
        // clone3's pointer arguments cannot be safely decoded in classic BPF.
        // ENOSYS permits the libc thread/fork fallback, whose flags are checked.
        out.extend([equal(435, 1), instruction(RET, UNSUPPORTED)]);
        for call in forbidden { out.extend([equal(*call, 1), instruction(RET, DENY)]); }
        out.extend([equal(clone, 5), instruction(LOAD, 16), instruction(AND, 0x7e020000),
            equal(0, 1), instruction(RET, ALLOW), instruction(RET, DENY)]);
        out.push(instruction(RET, ALLOW));
        let ioctl_start = out.len();
        out[2].k = (ioctl_start - 3) as u32;
        // ioctl(2) truncates its command to unsigned int. Compare the low
        // 32 bits on both ABIs, including high-bit native bypass attempts.
        out.push(instruction(LOAD, 24));
        for command in [0x401c5820, 0x40086602, 0x40046602] {
            out.extend([equal(command, 1), instruction(RET, DENY)]);
        }
        out.push(instruction(RET, ALLOW));
        out
    }
    #[cfg(target_arch = "x86_64")]
    let (native_arch, compat_arch, native, compat) = (0xc000003e, 0x40000003,
        abi(16,56,&[165,166,272,308,304,321,101,311,425,428,429,430,431,432,433,442]),
        abi(54,120,&[21,52,310,346,342,357,26,348,425,428,429,430,431,432,433,442]));
    #[cfg(target_arch = "aarch64")]
    let (native_arch, compat_arch, native, compat) = (0xc00000b7, 0x40000028,
        abi(29,220,&[40,39,97,268,265,280,117,271,425,428,429,430,431,432,433,442]),
        abi(54,120,&[21,52,337,375,371,386,26,377,425,428,429,430,431,432,433,442]));
    let mut code = vec![instruction(LOAD,4), equal(native_arch,1), instruction(JUMP,3),
        equal(compat_arch,1), instruction(JUMP,(native.len()+1) as u32), instruction(RET,KILL)];
    code.extend(native); code.extend(compat);
    #[cfg(target_arch = "x86_64")]
    {
        // x32 reports the native audit architecture but adds a syscall bit.
        // Reject that ABI explicitly; it must not bypass native ioctl checks.
        let offset = 6;
        code.insert(offset + 1, instruction(AND, 0x40000000));
        code.insert(offset + 2, equal(0,1));
        code.insert(offset + 3, instruction(JUMP,1));
        code.insert(offset + 4, instruction(RET,KILL));
        code.insert(offset + 5, instruction(LOAD,0));
        code[4].k += 5;
    }
    let program = sock_fprog { len: code.len().try_into().map_err(|_| error("worker seccomp bound"))?, filter:code.as_mut_ptr() };
    if unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0) } != 0 { return Err(io::Error::last_os_error()); }
    let result = unsafe { libc::syscall(libc::SYS_seccomp,1,1,&program) };
    if result != 0 { return Err(error(format!("worker seccomp TSYNC refused ({result})"))); }
    Ok(())
}

#[cfg(not(all(target_os = "linux", any(target_arch = "x86_64", target_arch = "aarch64"))))]
pub fn install() -> io::Result<()> { Err(error("hardened worker filter requires Linux x86_64 or aarch64")) }

/// A kernel check invoked from the reviewed immutable image before admission.
/// Invalid descriptors distinguish filter EPERM from ordinary kernel EBADF.
#[cfg(all(target_os = "linux", any(target_arch = "x86_64", target_arch = "aarch64")))]
pub fn verify() -> io::Result<serde_json::Value> {
    install()?;
    for command in [0x401c5820u64,0x40086602,0x40046602,0x1_401c5820,0x1_40086602] {
        let rc = unsafe { libc::syscall(libc::SYS_ioctl, -1, command, 0) };
        if rc != -1 || io::Error::last_os_error().raw_os_error() != Some(libc::EPERM) {
            return Err(error("kernel worker filter did not deny quota mutation"));
        }
    }
    let rc = unsafe { libc::syscall(libc::SYS_ioctl,-1,0x5401u64,0) };
    if rc != -1 || io::Error::last_os_error().raw_os_error() != Some(libc::EBADF) {
        return Err(error("worker filter did not preserve ordinary terminal ioctl"));
    }
    if unsafe { libc::prctl(libc::PR_GET_NO_NEW_PRIVS,0,0,0,0) } != 1 {
        return Err(error("hardened worker lacks no-new-privileges"));
    }
    Ok(serde_json::json!({"contract":CONTRACT,"quota_mutation_denied":true,
        "namespace_mutation_denied":true,"native_high_bits_denied":true,
        "compat_filter_present":true,"compat_execution_verified":false}))
}

#[cfg(not(all(target_os = "linux", any(target_arch = "x86_64", target_arch = "aarch64"))))]
pub fn verify() -> io::Result<serde_json::Value> { Err(error("hardened worker filter requires Linux x86_64 or aarch64")) }
