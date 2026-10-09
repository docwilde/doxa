//! Disposable guest exercise of the production connector-origin probe.
//! This creates real PID namespaces and cgroup membership, but no Docker
//! Engine; it cannot authenticate an Engine report or a later socket writer.
#[cfg(target_os = "linux")]
pub use doxa_isolation::error;
#[cfg(target_os = "linux")]
#[path = "../src/broker_origin.rs"]
mod broker_origin;

#[cfg(target_os = "linux")]
mod linux {
use super::{broker_origin, error};
use broker_origin::ContainerOriginPin;
use std::{fs, io, os::unix::net::{UnixListener, UnixStream},
    path::Path, time::{Duration, Instant}};

const SOCKET: &str = "/run/doxa-origin-guest.sock";
const GROUP: &str = "/sys/fs/cgroup/doxa-origin-guest";

struct GuestInit { pid: libc::pid_t, helper: libc::pid_t, ready: libc::c_int }

fn pipe() -> io::Result<[libc::c_int; 2]> {
    let mut fds = [-1; 2];
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(fds)
}

fn spawn_init() -> io::Result<GuestInit> {
    let pid_pipe = pipe()?;
    let ready_pipe = pipe()?;
    let helper = unsafe { libc::fork() };
    if helper < 0 { return Err(io::Error::last_os_error()); }
    if helper == 0 {
        unsafe { libc::close(pid_pipe[0]); libc::close(ready_pipe[1]); }
        if unsafe { libc::unshare(libc::CLONE_NEWPID) } != 0 { unsafe { libc::_exit(11); } }
        let init = unsafe { libc::fork() };
        if init < 0 { unsafe { libc::_exit(12); } }
        if init == 0 {
            unsafe { libc::close(pid_pipe[1]); }
            let mut ready = [0u8; 1];
            if unsafe { libc::read(ready_pipe[0], ready.as_mut_ptr().cast(), 1) } != 1 {
                unsafe { libc::_exit(13); }
            }
            if UnixStream::connect(SOCKET).is_err() { unsafe { libc::_exit(14); } }
            loop { unsafe { libc::pause(); } }
        }
        unsafe { libc::close(ready_pipe[0]); }
        let bytes = init.to_ne_bytes();
        if unsafe { libc::write(pid_pipe[1], bytes.as_ptr().cast(), bytes.len()) } != bytes.len() as isize {
            unsafe { libc::_exit(15); }
        }
        unsafe { libc::close(pid_pipe[1]); }
        let mut status = 0;
        if unsafe { libc::waitpid(init, &mut status, 0) } != init { unsafe { libc::_exit(16); } }
        unsafe { libc::_exit(0); }
    }
    unsafe { libc::close(pid_pipe[1]); libc::close(ready_pipe[0]); }
    let mut pid_bytes = [0u8; 4];
    let mut received = 0;
    while received < pid_bytes.len() {
        let count = unsafe { libc::read(pid_pipe[0], pid_bytes[received..].as_mut_ptr().cast(), pid_bytes.len() - received) };
        if count <= 0 { return Err(error("guest init PID handoff failed")); }
        received += count as usize;
    }
    unsafe { libc::close(pid_pipe[0]); }
    let pid = libc::pid_t::from_ne_bytes(pid_bytes);
    if pid <= 0 { return Err(error("guest init PID is invalid")); }
    fs::write(Path::new(GROUP).join("cgroup.procs"), pid.to_string())?;
    Ok(GuestInit { pid, helper, ready: ready_pipe[1] })
}

fn release(init: &GuestInit) -> io::Result<()> {
    let byte = [1u8];
    if unsafe { libc::write(init.ready, byte.as_ptr().cast(), 1) } != 1 {
        return Err(error("guest init release failed"));
    }
    unsafe { libc::close(init.ready); }
    Ok(())
}

fn accept(listener: &UnixListener) -> io::Result<UnixStream> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match listener.accept() {
            Ok((stream, _)) => return Ok(stream),
            Err(err) if err.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline =>
                std::thread::sleep(Duration::from_millis(5)),
            Err(err) => return Err(err),
        }
    }
}

fn stop(init: GuestInit) -> io::Result<()> {
    if unsafe { libc::kill(init.pid, libc::SIGKILL) } != 0 { return Err(io::Error::last_os_error()); }
    let mut status = 0;
    if unsafe { libc::waitpid(init.helper, &mut status, 0) } != init.helper || status != 0 {
        return Err(error("guest init helper did not exit cleanly"));
    }
    Ok(())
}

fn run() -> io::Result<()> {
    fs::create_dir(GROUP)?;
    let listener = UnixListener::bind(SOCKET)?;
    listener.set_nonblocking(true)?;
    let first = spawn_init()?;
    let first_pin = ContainerOriginPin::new(first.pid)?;
    release(&first)?;
    let worker = accept(&listener)?;
    first_pin.require_connector(&worker)?;
    println!("GUEST_CASE first_private_scope_accepted=PASS");

    let _host = UnixStream::connect(SOCKET)?;
    let host = accept(&listener)?;
    if first_pin.require_connector(&host).is_ok() { return Err(error("host connector accepted")); }
    println!("GUEST_CASE host_scope_refused=PASS");
    stop(first)?;
    if first_pin.require_connector(&worker).is_ok() { return Err(error("dead init pin accepted")); }
    println!("GUEST_CASE dead_init_refused=PASS");

    let second = spawn_init()?;
    let second_pin = ContainerOriginPin::new(second.pid)?;
    release(&second)?;
    let replacement = accept(&listener)?;
    if first_pin.require_connector(&replacement).is_ok() { return Err(error("old pin accepted replacement")); }
    second_pin.require_connector(&replacement)?;
    println!("GUEST_CASE replacement_requires_new_pin=PASS");
    stop(second)?;
    println!("DOXA_EGRESS_ORIGIN_GUEST_PASS cases=4 hardened_admission=false");
    Ok(())
}

pub fn main() {
    if let Err(err) = run() {
        eprintln!("DOXA_EGRESS_ORIGIN_GUEST_FAIL: {err}");
        std::process::exit(1);
    }
}
}

#[cfg(target_os = "linux")]
fn main() { linux::main(); }

#[cfg(not(target_os = "linux"))]
fn main() {}
