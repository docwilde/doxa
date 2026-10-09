//! Host-owned CONNECT gateway for a network-none session. Production wiring
//! remains gated on rootless and provider-flow integration smokes.
//!
//! This is deliberately not selected by any production isolation profile.
//! The worker can reach only its mounted Unix socket; the host resolves an
//! exact owner allowlist entry and connects to that resolved address itself.
use crate::{error, inspect_network, preflight, private_directory, read_manifest, Profile};
use std::{
    collections::HashSet,
    fs,
    io::{self, Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, Shutdown, SocketAddr, TcpListener, TcpStream, ToSocketAddrs},
    os::unix::{fs::{FileTypeExt, MetadataExt, PermissionsExt}, net::{UnixListener, UnixStream}},
    path::{Path, PathBuf},
    sync::{Arc, atomic::{AtomicBool, AtomicUsize, Ordering}},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const MAX_HEADER: usize = 8192;
const MAX_CLIENTS: usize = 32;
const IO_TIMEOUT: Duration = Duration::from_millis(200);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_CLIENT_HELLO: usize = 64 * 1024;

#[cfg(target_os = "linux")]
use std::os::{fd::{AsRawFd, FromRawFd, OwnedFd}, unix::ffi::OsStrExt};

type Resolver = dyn Fn(&str) -> io::Result<Vec<SocketAddr>> + Send + Sync;
type Connector = dyn Fn(SocketAddr) -> io::Result<TcpStream> + Send + Sync;

/// An exact, owner supplied hostname list. No wildcard or suffix matching.
#[derive(Clone)]
pub struct AllowedHosts(Arc<HashSet<String>>);
impl AllowedHosts {
    pub fn new(hosts: &[String]) -> io::Result<Self> {
        if hosts.is_empty() || hosts.len() > 32 { return Err(error("egress needs 1..32 exact hostnames")); }
        let mut names = HashSet::new();
        for host in hosts {
            let canonical = hostname(host)?;
            if !names.insert(canonical) { return Err(error("duplicate egress hostname")); }
        }
        Ok(Self(Arc::new(names)))
    }
    fn contains(&self, host: &str) -> bool { self.0.contains(host) }
}

fn hostname(value: &str) -> io::Result<String> {
    if value.len() > 253 || value.bytes().any(|b| !b.is_ascii()) || value.parse::<IpAddr>().is_ok() {
        return Err(error("egress target must be a DNS hostname"));
    }
    let host = value.to_ascii_lowercase();
    if !host.contains('.') || host.starts_with('.') || host.ends_with('.') ||
        host.split('.').any(|label| label.is_empty() || label.len() > 63 ||
            !label.as_bytes()[0].is_ascii_alphanumeric() ||
            !label.as_bytes()[label.len() - 1].is_ascii_alphanumeric() ||
            !label.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')) {
        return Err(error("invalid egress DNS hostname"));
    }
    Ok(host)
}

fn forbidden_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => {
            let n = u32::from(ip);
            // RFC 1918, link-local, shared/benchmark/documentation space,
            // multicast, future-use and IANA protocol assignments. These
            // exclusions are intentionally broader than routability.
            const BLOCKED: &[(u32, u8)] = &[
                (0x0000_0000, 8), (0x0a00_0000, 8), (0x6440_0000, 10),
                (0x7f00_0000, 8), (0xa9fe_0000, 16), (0xac10_0000, 12),
                (0xc000_0000, 24), (0xc000_0200, 24), (0xc058_6300, 24),
                (0xc0a8_0000, 16), (0xc612_0000, 15), (0xc633_6400, 24),
                (0xcb00_7100, 24), (0xe000_0000, 4), (0xf000_0000, 4),
            ];
            BLOCKED.iter().any(|&(prefix, bits)| n >> (32 - bits) == prefix >> (32 - bits))
        }
        IpAddr::V6(ip) => {
            // Permit ordinary global-unicast IPv6, excluding protocol and
            // documentation allocations. IPv4-mapped/translated, 6to4 and
            // NAT64 addresses cannot become a private-address bypass.
            let n = u128::from(ip);
            let in_prefix = |prefix: u128, bits: u32| n >> (128 - bits) == prefix >> (128 - bits);
            !in_prefix(u128::from(Ipv6Addr::new(0x2000,0,0,0,0,0,0,0)), 3)
                || in_prefix(u128::from(Ipv6Addr::new(0x2001,0,0,0,0,0,0,0)), 23)
                || in_prefix(u128::from(Ipv6Addr::new(0x2001,0xdb8,0,0,0,0,0,0)), 32)
                || in_prefix(u128::from(Ipv6Addr::new(0x2002,0,0,0,0,0,0,0)), 16)
        }
    }
}

fn parse_connect(bytes: &[u8]) -> io::Result<String> {
    let request = std::str::from_utf8(bytes).map_err(|_| error("CONNECT header must be ASCII"))?;
    if !request.is_ascii() || !request.ends_with("\r\n\r\n") { return Err(error("malformed CONNECT header")); }
    let mut lines = request[..request.len()-2].split("\r\n");
    let first = lines.next().ok_or_else(|| error("missing CONNECT request"))?;
    let mut words = first.split(' ');
    if words.next() != Some("CONNECT") { return Err(error("only CONNECT is supported")); }
    let authority = words.next().ok_or_else(|| error("missing CONNECT authority"))?;
    if words.next() != Some("HTTP/1.1") || words.next().is_some() { return Err(error("malformed CONNECT request line")); }
    let (host, port) = authority.rsplit_once(':').ok_or_else(|| error("CONNECT requires port 443"))?;
    if port != "443" { return Err(error("CONNECT permits only port 443")); }
    let host = hostname(host)?;
    let mut host_header = None;
    let mut count = 0;
    for line in lines {
        if line.is_empty() { continue; }
        count += 1;
        if count > 32 || line.starts_with([' ', '\t']) || line.bytes().any(|b| b < 32 || b == 127) {
            return Err(error("malformed CONNECT header field"));
        }
        let (name, value) = line.split_once(':').ok_or_else(|| error("malformed CONNECT header field"))?;
        if name.eq_ignore_ascii_case("host") {
            if host_header.is_some() { return Err(error("duplicate CONNECT Host header")); }
            host_header = Some(value.trim());
        } else if name.eq_ignore_ascii_case("content-length") || name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(error("CONNECT request body forbidden"));
        }
    }
    if host_header.is_none_or(|value| !value.eq_ignore_ascii_case(authority)) { return Err(error("CONNECT Host must match authority")); }
    Ok(host)
}

fn read_header(stream: &mut UnixStream) -> io::Result<Vec<u8>> {
    stream.set_read_timeout(Some(IO_TIMEOUT))?;
    let deadline = Instant::now() + CONNECT_TIMEOUT;
    let mut bytes = Vec::with_capacity(256);
    while bytes.len() < MAX_HEADER {
        if Instant::now() >= deadline { return Err(error("CONNECT header deadline exceeded")); }
        let mut one = [0];
        match stream.read(&mut one) {
            Ok(1) => bytes.push(one[0]),
            Ok(0) => return Err(error("incomplete CONNECT header")),
            Ok(_) => unreachable!(),
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => continue,
            Err(e) => return Err(e),
        }
        if bytes.ends_with(b"\r\n\r\n") { return Ok(bytes); }
    }
    Err(error("CONNECT header exceeds bound"))
}

// A CONNECT authority alone is insufficient: a TLS client can ask an allowed
// IP to serve a different SNI. Buffer the first handshake before sending any
// worker bytes upstream. Absent SNI, known ECH framing and unrecognised TLS
// records fail closed. Encrypted HTTP authority remains invisible to this
// gateway, so provider compatibility needs separate production proof.
fn read_exact_until(stream: &mut UnixStream, bytes: &mut [u8], deadline: Instant) -> io::Result<()> {
    let mut position = 0;
    while position < bytes.len() {
        if Instant::now() >= deadline { return Err(error("TLS ClientHello deadline exceeded")); }
        match stream.read(&mut bytes[position..]) {
            Ok(0) => return Err(error("incomplete TLS ClientHello")),
            Ok(size) => position += size,
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

fn take<'a>(bytes: &mut &'a [u8], count: usize) -> io::Result<&'a [u8]> {
    if bytes.len() < count { return Err(error("malformed TLS ClientHello")); }
    let (value, rest) = bytes.split_at(count); *bytes = rest; Ok(value)
}
fn take_u8(bytes: &mut &[u8]) -> io::Result<usize> { Ok(take(bytes, 1)?[0] as usize) }
fn take_u16(bytes: &mut &[u8]) -> io::Result<usize> {
    let value = take(bytes, 2)?; Ok(u16::from_be_bytes([value[0], value[1]]) as usize)
}

fn client_hello_sni(mut hello: &[u8]) -> io::Result<String> {
    take(&mut hello, 2 + 32)?; // legacy version and random
    let session = take_u8(&mut hello)?; take(&mut hello, session)?;
    let ciphers = take_u16(&mut hello)?;
    if ciphers < 2 || ciphers % 2 != 0 { return Err(error("malformed TLS cipher list")); }
    take(&mut hello, ciphers)?;
    let compression = take_u8(&mut hello)?;
    if compression == 0 { return Err(error("malformed TLS compression list")); }
    take(&mut hello, compression)?;
    let extensions = take_u16(&mut hello)?;
    if extensions != hello.len() { return Err(error("malformed TLS extensions")); }
    let mut found = None;
    while !hello.is_empty() {
        let kind = take_u16(&mut hello)?;
        let size = take_u16(&mut hello)?;
        let mut body = take(&mut hello, size)?;
        // ECH hides the inner name. Early data can carry encrypted application
        // bytes before the peer has authenticated; neither can be audited by
        // this SNI-only gateway. Resumption without early data remains valid.
        if kind == 0xfe0d { return Err(error("encrypted TLS ClientHello is unsupported")); }
        if kind == 42 { return Err(error("TLS early data is unsupported")); }
        if kind != 0 { continue; }
        if found.is_some() { return Err(error("duplicate TLS SNI extension")); }
        let names = take_u16(&mut body)?;
        if names != body.len() || names < 3 || take_u8(&mut body)? != 0 {
            return Err(error("invalid TLS SNI list"));
        }
        let size = take_u16(&mut body)?;
        let name = take(&mut body, size)?;
        if !body.is_empty() { return Err(error("multiple TLS SNI names")); }
        let name = std::str::from_utf8(name).map_err(|_| error("invalid TLS SNI name"))?;
        found = Some(hostname(name)?);
    }
    found.ok_or_else(|| error("TLS SNI is required"))
}

fn verified_client_hello(client: &mut UnixStream, host: &str) -> io::Result<Vec<u8>> {
    let deadline = Instant::now() + CONNECT_TIMEOUT;
    let mut records = Vec::new();
    let mut handshake = Vec::new();
    loop {
        let mut header = [0; 5]; read_exact_until(client, &mut header, deadline)?;
        let size = u16::from_be_bytes([header[3], header[4]]) as usize;
        if header[0] != 22 || header[1] != 3 || !(1..=4).contains(&header[2]) || size == 0 || size > 16 * 1024 {
            return Err(error("expected bounded TLS handshake record"));
        }
        let mut body = vec![0; size]; read_exact_until(client, &mut body, deadline)?;
        records.extend_from_slice(&header); records.extend_from_slice(&body);
        handshake.extend_from_slice(&body);
        if handshake.len() > MAX_CLIENT_HELLO { return Err(error("TLS ClientHello exceeds bound")); }
        if handshake.len() < 4 { continue; }
        if handshake[0] != 1 { return Err(error("expected TLS ClientHello")); }
        let size = (usize::from(handshake[1]) << 16) | (usize::from(handshake[2]) << 8) | usize::from(handshake[3]);
        if size > MAX_CLIENT_HELLO - 4 { return Err(error("TLS ClientHello exceeds bound")); }
        if handshake.len() < size + 4 { continue; }
        if handshake.len() != size + 4 { return Err(error("extra handshake bytes in TLS ClientHello record")); }
        if client_hello_sni(&handshake[4..size + 4])? != host { return Err(error("TLS SNI differs from CONNECT authority")); }
        return Ok(records);
    }
}

fn copy_until_end<R: Read, W: Write>(input: &mut R, output: &mut W, stop: &AtomicBool, peer_closed: Option<&AtomicBool>) -> io::Result<()> {
    let mut buffer = [0; 8192];
    while !stop.load(Ordering::Acquire) && !peer_closed.is_some_and(|closed| closed.load(Ordering::Acquire)) {
        match input.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(size) => output.write_all(&buffer[..size])?,
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

fn tunnel(mut client: UnixStream, mut upstream: TcpStream, stop: &AtomicBool) -> io::Result<()> {
    client.set_read_timeout(Some(IO_TIMEOUT))?;
    client.set_write_timeout(Some(CONNECT_TIMEOUT))?;
    upstream.set_read_timeout(Some(IO_TIMEOUT))?;
    upstream.set_write_timeout(Some(CONNECT_TIMEOUT))?;
    let mut client_read = client.try_clone()?;
    let mut upstream_read = upstream.try_clone()?;
    let cancel_upload = AtomicBool::new(false);
    thread::scope(|scope| {
        let upload = scope.spawn(|| { let result = copy_until_end(&mut client_read, &mut upstream, stop, Some(&cancel_upload)); let _ = upstream.shutdown(Shutdown::Write); result });
        let download = copy_until_end(&mut upstream_read, &mut client, stop, None);
        cancel_upload.store(true, Ordering::Release);
        let _ = client.shutdown(Shutdown::Write);
        upload.join().map_err(|_| io::Error::other("gateway upload panicked"))??;
        download
    })
}

// A blocking connect can wait forever behind a full Unix listener backlog.
// Only ECONNREFUSED proves that an owned socket inode has no listener; EAGAIN
// and every other failure must leave the existing path untouched.
#[cfg(target_os = "linux")]
fn stale_socket(socket: &Path) -> io::Result<bool> {
    let bytes = socket.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.len() >= address.sun_path.len() || bytes.contains(&0) {
        return Err(error("invalid egress socket path"));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (target, byte) in address.sun_path.iter_mut().zip(bytes) { *target = *byte as libc::c_char; }
    let descriptor = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0) };
    if descriptor < 0 { return Err(io::Error::last_os_error()); }
    let descriptor = unsafe { OwnedFd::from_raw_fd(descriptor) };
    if unsafe { libc::connect(descriptor.as_raw_fd(), (&raw const address).cast(), std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t) } == 0 {
        return Ok(false);
    }
    let err = io::Error::last_os_error();
    if err.raw_os_error() == Some(libc::ECONNREFUSED) { Ok(true) } else { Err(err) }
}

#[cfg(not(target_os = "linux"))]
fn stale_socket(_socket: &Path) -> io::Result<bool> {
    Err(error("stale egress socket reclamation requires Linux"))
}

fn serve(mut client: UnixStream, hosts: &AllowedHosts, resolver: &Resolver, connector: &Connector, stop: &AtomicBool) -> io::Result<()> {
    let request = match read_header(&mut client) {
        Ok(request) => request,
        Err(e) => { let _ = client.write_all(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"); return Err(e); }
    };
    let host = match parse_connect(&request) {
        Ok(host) if hosts.contains(&host) => host,
        Ok(_) => { let _ = client.write_all(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"); return Err(error("CONNECT host not allowlisted")); }
        Err(e) => { let _ = client.write_all(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"); return Err(e); }
    };
    // Resolve the absolute name once; connect to the checked SocketAddr,
    // never hand the hostname to a connector that could re-resolve it.
    let addresses = match resolver(&host) {
        Ok(addresses) => addresses,
        Err(e) => { let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"); return Err(e); }
    };
    if addresses.is_empty() || addresses.len() > 16 || addresses.iter().any(|a| a.port() != 443 || forbidden_ip(a.ip())) {
        let _ = client.write_all(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
        return Err(error("CONNECT DNS answer contains reserved address"));
    }
    if stop.load(Ordering::Acquire) { return Err(error("gateway stopped")); }
    client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")?;
    let hello = verified_client_hello(&mut client, &host)?;
    if stop.load(Ordering::Acquire) { return Err(error("gateway stopped")); }
    // Never open even an outbound TCP connection until the worker has sent a
    // bounded hello with the same exact SNI. The 200 response precedes the
    // upstream dial, so a failed dial closes the tunnel and surfaces as a TLS
    // error to the client rather than sending a second HTTP response.
    let mut upstream = addresses.into_iter().find_map(|address| connector(address).ok())
        .ok_or_else(|| error("CONNECT upstream unavailable"))?;
    if stop.load(Ordering::Acquire) { return Err(error("gateway stopped")); }
    // The checked IP is held in this socket; no second DNS lookup occurs.
    upstream.write_all(&hello)?;
    tunnel(client, upstream, stop)
}

/// Bound to a private session broker directory. Dropping it closes active
/// fixture tunnels and removes only the socket inode it created.
pub struct EgressGateway {
    socket: PathBuf, identity: (u64, u64), stop: Arc<AtomicBool>, worker: Option<JoinHandle<()>>,
}
impl EgressGateway {
    /// Reserved production entry point. It cannot open a socket until quota
    /// enforcement is verified on this exact session after restart. A fixture
    /// receipt is insufficient, even if a caller changes its Boolean fields.
    pub fn start_hardened_for_session(manifest_path: &Path, quota_receipt: &[u8], hosts: AllowedHosts) -> io::Result<Self> {
        let manifest = read_manifest(manifest_path)?;
        crate::hardened::require_session_hard_quota(&manifest, quota_receipt)?;
        Self::start_for_session(manifest_path, hosts)
    }
    /// Guarded host preparation path. It refuses a saved profile with any
    /// worker network, an unverified rootless Engine, or an altered container.
    /// The caller must retain this handle for the complete session lifetime.
    pub fn start_for_session(manifest_path: &Path, hosts: AllowedHosts) -> io::Result<Self> {
        let manifest = read_manifest(manifest_path)?;
        if manifest.profile != Profile::DockerOffline || manifest.state != "ready" {
            return Err(error("restricted egress requires a ready network-none Docker session"));
        }
        preflight(manifest.policy.as_ref().ok_or_else(|| error("Docker policy missing"))?)?;
        inspect_network(&manifest)?;
        let gateway = Self::start(&manifest.broker, hosts)?;
        // A failed post-bind check drops the new socket instead of advertising
        // a gateway for a container whose network changed meanwhile.
        inspect_network(&manifest)?;
        Ok(gateway)
    }
    pub fn start(broker_dir: &Path, hosts: AllowedHosts) -> io::Result<Self> {
        let resolver = Arc::new(|host: &str| format!("{host}.:443").to_socket_addrs().map(|rows| rows.collect()));
        let connector = Arc::new(|address| TcpStream::connect_timeout(&address, CONNECT_TIMEOUT));
        Self::start_with(broker_dir, hosts, resolver, connector)
    }
    fn start_with(broker_dir: &Path, hosts: AllowedHosts, resolver: Arc<Resolver>, connector: Arc<Connector>) -> io::Result<Self> {
        private_directory(broker_dir, false)?;
        let socket = broker_dir.join("egress.sock");
        if let Ok(meta) = fs::symlink_metadata(&socket) {
            if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } { return Err(error("unsafe stale egress socket")); }
            if !stale_socket(&socket)? { return Err(error("egress gateway already live")); }
            let current = fs::symlink_metadata(&socket)?;
            if (current.dev(), current.ino()) != (meta.dev(), meta.ino()) || !current.file_type().is_socket() {
                return Err(error("egress socket changed during stale check"));
            }
            fs::remove_file(&socket)?;
        }
        let listener = UnixListener::bind(&socket)?;
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let meta = fs::metadata(&socket)?;
        let identity = (meta.dev(), meta.ino());
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let worker = thread::spawn(move || {
            let active = Arc::new(AtomicUsize::new(0));
            let mut clients: Vec<JoinHandle<()>> = Vec::new();
            while !stopping.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(connection) => connection,
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                        let mut index = 0;
                        while index < clients.len() {
                            if clients[index].is_finished() { let _ = clients.swap_remove(index).join(); } else { index += 1; }
                        }
                        thread::sleep(Duration::from_millis(20)); continue;
                    }
                    Err(_) => break,
                };
                if active.load(Ordering::Acquire) >= MAX_CLIENTS {
                    let _ = stream.write_all(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n"); continue;
                }
                active.fetch_add(1, Ordering::AcqRel);
                let (hosts, resolver, connector, stopping, active) = (hosts.clone(), resolver.clone(), connector.clone(), stopping.clone(), active.clone());
                clients.push(thread::spawn(move || {
                    let _ = serve(stream, &hosts, resolver.as_ref(), connector.as_ref(), &stopping);
                    active.fetch_sub(1, Ordering::AcqRel);
                }));
                let mut index = 0;
                while index < clients.len() {
                    if clients[index].is_finished() { let _ = clients.swap_remove(index).join(); } else { index += 1; }
                }
            }
            for client in clients { let _ = client.join(); }
        });
        Ok(Self { socket, identity, stop, worker: Some(worker) })
    }
    pub fn socket(&self) -> &Path { &self.socket }
}
impl Drop for EgressGateway {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() { let _ = worker.join(); }
        if fs::symlink_metadata(&self.socket).is_ok_and(|m| (m.dev(),m.ino()) == self.identity) { let _ = fs::remove_file(&self.socket); }
    }
}

/// Fixture adapter inside a network-none worker. The only TCP listener is
/// loopback; it forwards the raw proxy stream to the mounted host socket.
/// A dead host gateway yields 502, never a direct network fallback.
pub fn serve_loopback_adapter(port: u16) -> io::Result<()> {
    if port < 1024 { return Err(error("fixture proxy port must be unprivileged")); }
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, port))?;
    let active = Arc::new(AtomicUsize::new(0));
    for connection in listener.incoming() {
        let mut client = connection?;
        if active.load(Ordering::Acquire) >= MAX_CLIENTS {
            let _ = client.write_all(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n"); continue;
        }
        active.fetch_add(1, Ordering::AcqRel);
        let active = active.clone();
        thread::spawn(move || {
            let _ = bridge_loopback_client(client, Path::new("/run/doxa/session/egress.sock"));
            active.fetch_sub(1, Ordering::AcqRel);
        });
    }
    Ok(())
}

fn bridge_loopback_client(mut client: TcpStream, socket: &Path) -> io::Result<()> {
    let mut host = match UnixStream::connect(socket) {
        Ok(host) => host,
        Err(e) => { let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"); return Err(e); }
    };
    client.set_read_timeout(Some(IO_TIMEOUT))?;
    client.set_write_timeout(Some(CONNECT_TIMEOUT))?;
    host.set_read_timeout(Some(IO_TIMEOUT))?;
    host.set_write_timeout(Some(CONNECT_TIMEOUT))?;
    let mut upload_client = client.try_clone()?;
    let mut upload_host = host.try_clone()?;
    let stop = AtomicBool::new(false);
    let cancel_upload = AtomicBool::new(false);
    thread::scope(|scope| {
        let upload = scope.spawn(|| { let result = copy_until_end(&mut upload_client, &mut upload_host, &stop, Some(&cancel_upload)); let _ = upload_host.shutdown(Shutdown::Write); result });
        let download = copy_until_end(&mut host, &mut client, &stop, None);
        cancel_upload.store(true, Ordering::Release);
        let _ = client.shutdown(Shutdown::Write);
        upload.join().map_err(|_| io::Error::other("fixture proxy upload panicked"))??;
        download
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{os::unix::fs::PermissionsExt, sync::atomic::AtomicUsize};

    fn fixture_dir() -> tempfile::TempDir {
        // The checkout may exceed AF_UNIX's path budget in an agent worktree.
        // Callers choose a short real-disk TMPDIR for these socket fixtures.
        let root = tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        root
    }
    fn hosts() -> AllowedHosts { AllowedHosts::new(&["api.example.test".to_owned()]).unwrap() }
    fn gateway(root: &Path, addresses: Vec<SocketAddr>, upstream: SocketAddr, calls: Arc<AtomicUsize>) -> EgressGateway {
        EgressGateway::start_with(root, hosts(),
            Arc::new(move |_| Ok(addresses.clone())),
            Arc::new(move |_| { calls.fetch_add(1, Ordering::AcqRel); TcpStream::connect(upstream) })).unwrap()
    }
    fn request(socket: &Path, bytes: &[u8]) -> Vec<u8> {
        let mut stream = UnixStream::connect(socket).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        stream.write_all(bytes).unwrap();
        stream.shutdown(Shutdown::Write).unwrap();
        let mut result = Vec::new(); stream.read_to_end(&mut result).unwrap(); result
    }
    fn upstream() -> (SocketAddr, JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let worker = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut bytes = Vec::new(); stream.read_to_end(&mut bytes).unwrap();
            stream.write_all(&bytes).unwrap();
            bytes
        });
        (address, worker)
    }
    fn read_response_header<R: Read>(stream: &mut R) -> String {
        let mut bytes = Vec::new();
        loop {
            let mut one = [0]; stream.read_exact(&mut one).unwrap(); bytes.push(one[0]);
            if bytes.ends_with(b"\r\n\r\n") { break; }
        }
        String::from_utf8(bytes).unwrap()
    }
    fn client_hello(name: &str) -> Vec<u8> {
        let mut server_name = vec![0];
        server_name.extend_from_slice(&(name.len() as u16).to_be_bytes());
        server_name.extend_from_slice(name.as_bytes());
        let mut sni = Vec::new();
        sni.extend_from_slice(&(server_name.len() as u16).to_be_bytes());
        sni.extend_from_slice(&server_name);
        let mut body = vec![3, 3]; body.extend_from_slice(&[0; 32]);
        body.extend_from_slice(&[0, 0, 2, 0x13, 1, 1, 0]);
        body.extend_from_slice(&((sni.len() + 4) as u16).to_be_bytes());
        body.extend_from_slice(&[0, 0]);
        body.extend_from_slice(&(sni.len() as u16).to_be_bytes());
        body.extend_from_slice(&sni);
        let len = body.len();
        let mut handshake = vec![1, (len >> 16) as u8, (len >> 8) as u8, len as u8];
        handshake.extend_from_slice(&body);
        let mut record = vec![22, 3, 3];
        record.extend_from_slice(&(handshake.len() as u16).to_be_bytes());
        record.extend_from_slice(&handshake);
        record
    }

    #[test]
    fn exact_connect_and_binary_tls_bytes_relay_via_loopback_fixture() {
        let root = fixture_dir(); let (upstream_addr, upstream_thread) = upstream();
        let calls = Arc::new(AtomicUsize::new(0));
        let allowed: SocketAddr = "1.1.1.1:443".parse().unwrap();
        let gateway = gateway(root.path(), vec![allowed], upstream_addr, calls.clone());
        assert_eq!(fs::metadata(gateway.socket()).unwrap().permissions().mode() & 0o777, 0o600);
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let proxy_addr = listener.local_addr().unwrap(); let socket = gateway.socket().to_owned();
        let bridge = thread::spawn(move || { let (client, _) = listener.accept().unwrap(); bridge_loopback_client(client, &socket).unwrap(); });
        let mut client = TcpStream::connect(proxy_addr).unwrap();
        client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        client.write_all(b"CONNECT API.EXAMPLE.TEST:443 HTTP/1.1\r\nHost: API.EXAMPLE.TEST:443\r\n\r\n").unwrap();
        assert_eq!(read_response_header(&mut client), "HTTP/1.1 200 Connection Established\r\n\r\n");
        let mut tls_like = client_hello("API.EXAMPLE.TEST");
        tls_like.extend_from_slice(&[0, 0xff, 0x80, 0x13]);
        client.write_all(&tls_like).unwrap(); client.shutdown(Shutdown::Write).unwrap();
        let mut echoed = Vec::new(); client.read_to_end(&mut echoed).unwrap();
        assert_eq!(echoed, tls_like); assert_eq!(upstream_thread.join().unwrap(), tls_like);
        bridge.join().unwrap(); assert_eq!(calls.load(Ordering::Acquire), 1);
        drop(gateway); assert!(!root.path().join("egress.sock").exists());
    }

    #[test]
    fn mismatched_or_missing_sni_never_opens_an_upstream_connection() {
        for hello in [client_hello("other.example.test"), vec![22, 3, 3, 0, 4, 1, 0, 0, 0]] {
            let root = fixture_dir(); let calls = Arc::new(AtomicUsize::new(0));
            let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()],
                "127.0.0.1:1".parse().unwrap(), calls.clone());
            let mut bytes = b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n".to_vec();
            bytes.extend_from_slice(&hello);
            assert!(request(gateway.socket(), &bytes).starts_with(b"HTTP/1.1 200"));
            assert_eq!(calls.load(Ordering::Acquire), 0);
        }
    }

    fn append_tls_extension(mut hello: Vec<u8>, kind: u16, body: &[u8]) -> Vec<u8> {
        hello.extend_from_slice(&kind.to_be_bytes());
        hello.extend_from_slice(&(body.len() as u16).to_be_bytes());
        hello.extend_from_slice(body);
        let record_size = (hello.len() - 5) as u16;
        let handshake_size = hello.len() - 9;
        let extensions_size = (hello.len() - 52) as u16;
        hello[3..5].copy_from_slice(&record_size.to_be_bytes());
        hello[6..9].copy_from_slice(&[(handshake_size >> 16) as u8, (handshake_size >> 8) as u8, handshake_size as u8]);
        hello[50..52].copy_from_slice(&extensions_size.to_be_bytes());
        hello
    }

    #[test]
    fn early_data_and_extra_handshake_bytes_fail_before_dial() {
        let hello = client_hello("api.example.test");
        let early_data = append_tls_extension(hello.clone(), 42, &[]);
        let mut extra_handshake = hello;
        extra_handshake.extend_from_slice(&[0, 0, 0, 0]);
        let extra_size = (extra_handshake.len() - 5) as u16;
        extra_handshake[3..5].copy_from_slice(&extra_size.to_be_bytes());
        for malformed in [early_data, extra_handshake] {
            let root = fixture_dir(); let calls = Arc::new(AtomicUsize::new(0));
            let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()],
                "127.0.0.1:1".parse().unwrap(), calls.clone());
            let mut request_bytes = b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n".to_vec();
            request_bytes.extend_from_slice(&malformed);
            assert!(request(gateway.socket(), &request_bytes).starts_with(b"HTTP/1.1 200"));
            assert_eq!(calls.load(Ordering::Acquire), 0);
        }
    }

    #[test]
    fn fragmented_client_hello_is_relayed_after_matching_sni() {
        let hello = client_hello("api.example.test");
        let mut fragmented = hello[..8].to_vec();
        fragmented[3..5].copy_from_slice(&3u16.to_be_bytes());
        fragmented.extend_from_slice(&hello[..3]);
        fragmented.extend_from_slice(&((hello.len() - 8) as u16).to_be_bytes());
        fragmented.extend_from_slice(&hello[8..]);
        assert_eq!(client_hello_sni(&hello[9..]).unwrap(), "api.example.test");
        let root = fixture_dir(); let (upstream_addr, upstream_thread) = upstream();
        let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()],
            upstream_addr, Arc::new(AtomicUsize::new(0)));
        let mut bytes = b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n".to_vec();
        bytes.extend_from_slice(&fragmented);
        assert!(request(gateway.socket(), &bytes).starts_with(b"HTTP/1.1 200"));
        assert_eq!(upstream_thread.join().unwrap(), fragmented);
    }

    #[test]
    fn malformed_unknown_and_non_443_connects_never_reach_upstream() {
        let root = fixture_dir(); let calls = Arc::new(AtomicUsize::new(0));
        let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()],
            "127.0.0.1:1".parse().unwrap(), calls.clone());
        for bad in [
            b"CONNECT other.example.test:443 HTTP/1.1\r\nHost: other.example.test:443\r\n\r\n".as_slice(),
            b"CONNECT api.example.test:80 HTTP/1.1\r\nHost: api.example.test:80\r\n\r\n",
            b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1:443\r\n\r\n",
            b"CONNECT [::1]:443 HTTP/1.1\r\nHost: [::1]:443\r\n\r\n",
            b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: other.example.test:443\r\n\r\n",
            b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\nHost: api.example.test:443\r\n\r\n",
            b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\nContent-Length: 1\r\n\r\n",
            b"GET http://api.example.test/ HTTP/1.1\r\nHost: api.example.test\r\n\r\n",
        ] {
            let response = request(gateway.socket(), bad);
            assert!(response.starts_with(b"HTTP/1.1 400") || response.starts_with(b"HTTP/1.1 403"), "{response:?}");
        }
        assert_eq!(calls.load(Ordering::Acquire), 0);
    }

    #[test]
    fn reserved_or_mixed_dns_answers_fail_closed_before_connect() {
        for addresses in [
            vec!["127.0.0.1:443".parse().unwrap()],
            vec!["1.1.1.1:443".parse().unwrap(), "10.1.2.3:443".parse().unwrap()],
            vec!["[::1]:443".parse().unwrap()],
            vec!["[2001:db8::1]:443".parse().unwrap()],
            vec![],
        ] {
            let root = fixture_dir(); let calls = Arc::new(AtomicUsize::new(0));
            let gateway = gateway(root.path(), addresses, "127.0.0.1:1".parse().unwrap(), calls.clone());
            let response = request(gateway.socket(), b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n");
            assert!(response.starts_with(b"HTTP/1.1 403"), "{response:?}");
            assert_eq!(calls.load(Ordering::Acquire), 0);
        }
        for ip in ["0.0.0.1", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.1.1",
            "172.16.0.1", "192.0.0.1", "192.0.2.1", "192.168.0.1", "198.18.0.1",
            "198.51.100.1", "203.0.113.1", "224.0.0.1", "240.0.0.1", "255.255.255.255",
            "::1", "fc00::1", "fe80::1", "2001:db8::1", "2002::1", "::ffff:127.0.0.1"] {
            assert!(forbidden_ip(ip.parse().unwrap()), "{ip}");
        }
        assert!(!forbidden_ip("1.1.1.1".parse().unwrap()));
        assert!(!forbidden_ip("2606:4700:4700::1111".parse().unwrap()));
    }

    #[test]
    fn dead_gateway_and_unsafe_socket_fail_closed() {
        let root = fixture_dir(); let calls = Arc::new(AtomicUsize::new(0));
        let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()],
            "127.0.0.1:1".parse().unwrap(), calls);
        assert!(EgressGateway::start(root.path(), hosts()).is_err());
        drop(gateway);
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap(); let socket = root.path().join("egress.sock");
        let bridge = thread::spawn(move || { let (client, _) = listener.accept().unwrap(); assert!(bridge_loopback_client(client, &socket).is_err()); });
        let mut client = TcpStream::connect(address).unwrap();
        assert_eq!(read_response_header(&mut client), "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        bridge.join().unwrap();
        std::os::unix::fs::symlink("/etc/passwd", root.path().join("egress.sock")).unwrap();
        assert!(EgressGateway::start(root.path(), hosts()).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn full_live_socket_backlog_is_not_reclaimed() {
        use std::os::fd::AsRawFd;
        let root = fixture_dir();
        let socket = root.path().join("egress.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 0) }, 0);
        let queued = UnixStream::connect(&socket).unwrap();
        let identity = fs::metadata(&socket).unwrap();
        let path = root.path().to_owned();
        let (done, result) = std::sync::mpsc::channel();
        let attempt = thread::spawn(move || { let _ = done.send(EgressGateway::start(&path, hosts()).is_err()); });
        let bounded_result = result.recv_timeout(Duration::from_secs(2));
        let after = fs::metadata(&socket).unwrap();
        drop(queued);
        drop(listener);
        if bounded_result.is_ok() { attempt.join().unwrap(); }
        assert_eq!(bounded_result.unwrap(), true, "live-socket check blocked behind its backlog");
        assert_eq!((after.dev(), after.ino()), (identity.dev(), identity.ino()));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn refused_owned_socket_is_reclaimed() {
        let root = fixture_dir();
        let socket = root.path().join("egress.sock");
        let stale = UnixListener::bind(&socket).unwrap();
        drop(stale);
        assert!(stale_socket(&socket).unwrap());
        let gateway = EgressGateway::start(root.path(), hosts()).unwrap();
        assert!(!stale_socket(gateway.socket()).unwrap());
        drop(gateway);
        assert!(!socket.exists());
    }

    #[test]
    fn stopping_gateway_closes_an_existing_tunnel() {
        let root = fixture_dir();
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let hello = client_hello("api.example.test");
        let hello_len = hello.len();
        let (received, ready) = std::sync::mpsc::channel();
        let upstream_thread = thread::spawn(move || {
            let (mut upstream, _) = listener.accept().unwrap();
            upstream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut bytes = vec![0; hello_len]; upstream.read_exact(&mut bytes).unwrap();
            received.send(()).unwrap();
            upstream.read_to_end(&mut bytes).unwrap(); bytes
        });
        let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()], address, Arc::new(AtomicUsize::new(0)));
        let mut client = UnixStream::connect(gateway.socket()).unwrap();
        client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        client.write_all(b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n").unwrap();
        assert!(read_response_header(&mut client).starts_with("HTTP/1.1 200"));
        client.write_all(&hello).unwrap();
        ready.recv_timeout(Duration::from_secs(3)).unwrap();
        drop(gateway);
        let mut remaining = Vec::new(); client.read_to_end(&mut remaining).unwrap();
        assert!(remaining.is_empty());
        assert_eq!(upstream_thread.join().unwrap(), hello);
    }

    #[test]
    fn owner_allowlist_rejects_literals_wildcards_and_duplicates() {
        for host in ["127.0.0.1", "[::1]", "*.example.com", "example.com.", "a..example.com", "localhost", "a/b.example.com"] {
            assert!(AllowedHosts::new(&[host.to_owned()]).is_err(), "{host}");
        }
        assert!(AllowedHosts::new(&["EXAMPLE.COM".into(), "example.com".into()]).is_err());
    }

    #[test]
    fn ambiguous_tls_name_extensions_are_refused() {
        let original = client_hello("api.example.test");
        let extension = original[52..].to_vec();
        for extra in [extension, vec![0xfe, 0x0d, 0, 0]] {
            let mut record = original.clone();
            record.extend_from_slice(&extra);
            let record_size = (record.len() - 5) as u16;
            let handshake_size = record.len() - 9;
            let extensions_size = (record.len() - 52) as u16;
            record[3..5].copy_from_slice(&record_size.to_be_bytes());
            record[6..9].copy_from_slice(&[(handshake_size >> 16) as u8, (handshake_size >> 8) as u8, handshake_size as u8]);
            record[50..52].copy_from_slice(&extensions_size.to_be_bytes());
            assert!(client_hello_sni(&record[9..]).is_err());
        }
    }

    #[test]
    fn guarded_gateway_refuses_non_docker_manifest_before_socket_binding() {
        let root = fixture_dir();
        let session = root.path().join("session");
        fs::create_dir(&session).unwrap();
        fs::set_permissions(&session, fs::Permissions::from_mode(0o700)).unwrap();
        let manifest = crate::Manifest {
            version: 1, session_id: "session".into(), profile: Profile::Native,
            policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
            source: root.path().to_owned(), checkout: root.path().to_owned(),
            context_cwd: None, provider_rollout: None, checkout_device: 0,
            checkout_inode: 0, base_sha: String::new(), branch: String::new(),
            private_home: root.path().to_owned(), cache: root.path().to_owned(),
            broker: session.clone(), container_id: None, nonce: String::new(),
            state: "ready".into(),
        };
        let path = session.join("manifest.json");
        fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let error = EgressGateway::start_for_session(&path, hosts()).err().unwrap();
        assert!(error.to_string().contains("network-none Docker session"));
        let error = EgressGateway::start_hardened_for_session(&path, b"{}", hosts()).err().unwrap();
        assert!(error.to_string().contains("network-none Docker session"));
        assert!(!session.join("egress.sock").exists());
    }
}
