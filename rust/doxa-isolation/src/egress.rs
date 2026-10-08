//! Fixture-only, host-owned CONNECT gateway for a network-none session.
//!
//! This is deliberately not selected by any production isolation profile.
//! The worker can reach only its mounted Unix socket; the host resolves an
//! exact owner allowlist entry and connects to that resolved address itself.
use crate::{error, private_directory};
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
    let upstream = match addresses.into_iter().find_map(|address| connector(address).ok()) {
        Some(stream) => stream,
        None => { let _ = client.write_all(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"); return Err(error("CONNECT upstream unavailable")); }
    };
    if stop.load(Ordering::Acquire) { return Err(error("gateway stopped")); }
    client.write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")?;
    tunnel(client, upstream, stop)
}

/// Bound to a private session broker directory. Dropping it closes active
/// fixture tunnels and removes only the socket inode it created.
pub struct EgressGateway {
    socket: PathBuf, identity: (u64, u64), stop: Arc<AtomicBool>, worker: Option<JoinHandle<()>>,
}
impl EgressGateway {
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
            if UnixStream::connect(&socket).is_ok() { return Err(error("egress gateway already live")); }
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
        let root = tempfile::tempdir_in(std::env::current_dir().unwrap()).unwrap();
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
        let tls_like = [0x16,0x03,0x03,0x00,0x05,0,0xff,0x80,0x13];
        client.write_all(&tls_like).unwrap(); client.shutdown(Shutdown::Write).unwrap();
        let mut echoed = Vec::new(); client.read_to_end(&mut echoed).unwrap();
        assert_eq!(echoed, tls_like); assert_eq!(upstream_thread.join().unwrap(), tls_like);
        bridge.join().unwrap(); assert_eq!(calls.load(Ordering::Acquire), 1);
        drop(gateway); assert!(!root.path().join("egress.sock").exists());
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

    #[test]
    fn stopping_gateway_closes_an_existing_tunnel() {
        let root = fixture_dir();
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let upstream_thread = thread::spawn(move || {
            let (mut upstream, _) = listener.accept().unwrap();
            upstream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
            let mut bytes = Vec::new(); upstream.read_to_end(&mut bytes).unwrap(); bytes
        });
        let gateway = gateway(root.path(), vec!["1.1.1.1:443".parse().unwrap()], address, Arc::new(AtomicUsize::new(0)));
        let mut client = UnixStream::connect(gateway.socket()).unwrap();
        client.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        client.write_all(b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\n").unwrap();
        assert!(read_response_header(&mut client).starts_with("HTTP/1.1 200"));
        drop(gateway);
        let mut remaining = Vec::new(); client.read_to_end(&mut remaining).unwrap();
        assert!(remaining.is_empty());
        assert!(upstream_thread.join().unwrap().is_empty());
    }

    #[test]
    fn owner_allowlist_rejects_literals_wildcards_and_duplicates() {
        for host in ["127.0.0.1", "[::1]", "*.example.com", "example.com.", "a..example.com", "localhost", "a/b.example.com"] {
            assert!(AllowedHosts::new(&[host.to_owned()]).is_err(), "{host}");
        }
        assert!(AllowedHosts::new(&["EXAMPLE.COM".into(), "example.com".into()]).is_err());
    }
}
