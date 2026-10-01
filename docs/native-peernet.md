# Native remote peer bridge

The native bridge uses Hyper HTTP/1 over a private Unix socket and reqwest for
outbound endpoint requests. It serves only `POST /peers`, with `roster`, `history`,
and `deliver` operations. Request and response JSON are limited to 64 KiB.
Header parsing and request completion have deadlines; redirects are refused.
Original local delivery, scoped discovery, scrubbing, rate limits, and successful
recipient ledger records remain authoritative.

## Configuration

Remote listening is off by default. Set `DOXA_REMOTE_ENABLED=1` and a comma
separated `DOXA_REMOTE_ALLOWED_LOGINS` list to admit remote callers. Empty lists
deny everyone. The corresponding `config.toml` settings are `remote_enabled` and
`remote_allowed_logins`; environment values take precedence.

Configure Tailscale Serve to forward to the private runtime `peernet.sock`.
The bridge checks Linux `SO_PEERCRED` or macOS `getpeereid` before parsing HTTP and requires the
configured proxy UID (`DOXA_REMOTE_PROXY_UID`, default `0`). An ordinary user's
own UID cannot be selected as the proxy. A TCP loopback connection or forwarded
identity header cannot establish proxy identity. Only the attested proxy's
`Tailscale-User-Login` header is checked against the allow list. Duplicate login
headers and unsupported body encodings are refused. Shell and permission bypass
policy remain separately denied by default, and this bridge exposes neither
operation.

Set `DOXA_REMOTE_PEERS=machine=host.tailnet.ts.net:47600,...` (or `remote_peers`)
for outbound discovery and delivery. Entries require a simple hostname and a
valid port; malformed entries are ignored. Requests use the configured endpoint
directly, without environment HTTP proxies, caller identity headers, or redirects.

**Reciprocal endpoint configuration is required for message admission.** A
receiving native session accepts a remote sender only when its exact session ID
appears uniquely in the current roster fetched from its own configured endpoints.
This strengthens the legacy one way endpoint setup: configure each sending and
receiving machine to discover the other. A payload's claimed machine/origin is
never authority. Displayed origins are derived from the configured endpoint that
DOXA actually queried. Remote roster rows cannot supply local socket paths, PIDs,
or daemon sockets.

The shared service exits after the local registry remains empty for 75 seconds.
Startup and shutdown coordinate through an owned private lock. Cleanup removes
only the socket inode created by that service; replacing the socket does not
allow an older service to remove the replacement.
