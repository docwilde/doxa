# Inactive quota helper package

These template units package the read-only `doxa-quota-helper` service. They
are not installed or enabled by the build. `%i` is a DOXA session ID containing
only ASCII letters, digits and hyphens. The socket is root-owned, mode `0660`,
with group `doxa-quota-readers`; the helper still requires the exact caller UID
from a root-owned policy. The service runs as root with only `CAP_SYS_ADMIN`
in its capability bounding set because the project-quota query needs it.

An administrator must complete these actions for one isolated test session:

1. Provision a dedicated, non-root caller account and a different non-root
   session-tree owner. Make the caller a member of `doxa-quota-readers`. The
   current same-UID controller/worker deployment cannot satisfy this split.
2. On an XFS or ext4 project-quota mount, configure a finite project hard
   limit and matching project inheritance for the private session root,
   checkout, home, cache and broker directories. Record their exact device,
   inode and mount IDs in `/etc/doxa/quota/SESSION_ID.json`, root-owned mode
   `0600`. Its `socket_path` must be `/run/doxa/quota/SESSION_ID.sock` and its
   `caller_uid` and `owner_uid` must match the separate accounts.
3. Build the reviewed source with
   `cargo build --locked --release -p doxa-isolation --bin doxa-quota-helper --bin doxa-quota-install-preflight`.
   Install both binaries under
   `/usr/libexec/doxa/`, root-owned mode `0755`, and install both templates
   under `/etc/systemd/system/`, root-owned mode `0644`. Every ancestor must
   be root-owned and not group/other writable; `/usr/libexec/doxa` must be a
   real directory, not a symlink. Do not enable the socket yet.
4. Record the SHA-256 of the reviewed helper build before installation and
   compare it with the staged artifact. Run `systemd-analyze verify` on the
   two staged unit files, then run
   `/usr/libexec/doxa/doxa-quota-install-preflight SESSION_ID --reviewed-helper-sha256 DIGEST`
   as root, with that independently recorded lowercase digest. It
   reads the staged files and current quota state without starting the
   service. It refuses a live socket, changed template, several exact
   template/instance overrides, an unsafe helper binary or digest,
   unavailable caller group, wrong policy or quota snapshot. The report says
   `effective_unit_verified=false`: systemd also applies dash-prefix drop-ins
   and other unit search paths that this preflight does not enumerate.

The preflight result always has `admissible_as_hard_quota=false`. An operator
must separately review systemd's effective unit configuration, installed-host
socket activation, the caller/tree-owner runtime split, EDQUOT through every
bind after restart/remount, exact Docker Engine and broker writer origin, and
real provider egress. Only then can a later release consider hardened
admission. No command here enables or starts a host unit.
