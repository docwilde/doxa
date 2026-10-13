# Caller transport for the ROOT session broker

`root_client` is the caller-side transport for the administrator-installed
broker. `installed(session_id)` accepts only the fixed
`/run/doxa/hardened/SESSION_ID/broker-receipt.json`. The receipt and public
policy must be root-owned, mode0444, regular single-link files reached through
root-owned ancestors without symlinks or group/other write permissions.
The client retains descriptors and checks inode/content identity while reading
the receipt, policy, script and real interpreter. Session, caller UID, current
kernel boot, policy hash and script/interpreter hashes must agree.

Each operation executes the pinned interpreter with `-I`, the pinned script,
and `client --session-id ID --op OPERATION`. The isolated Python client checks
its installation again and authenticates the current root peer using kernel
peer credentials/PIDFD, executable and exact script/policy command line. A
receipt or metadata reply is not admission. The ROOT broker composes live
Engine, native gateway, quota and exec-filter checks. There is no Docker or
original host-program fallback.

The Rust APIs return `io::Result`: `installed`, `control`, `prepare`,
`provider_command`, `workspace_command`, `register_hook`, `snapshot` and
`verify_snapshot`. Build transformed commands before configuring stdio,
process groups or other spawn options; `Command` exposes no getters for those
settings. Lifecycle replies must match the installed session/policy;
prepare/resume require a ready container identity. The callback capability is
48 lowercase hex characters, sent through `DOXA_ROOT_HOOK_CAPABILITY` and the
authenticated socket rather than argv.

`prepare` stages the initial independent checkout and only the prepared home
inventory. Claude inputs are `.credentials.json`, `settings.json`,
`.doxa-logged-out`, `skills` and `adopted-plugins`; Codex input is `auth.json`.
Runtime history, generic home files and provider configuration are not
uploaded. Inputs must be caller-owned and private; symlinks, hardlinks, special
entries, Git/hooks/MCP entries in snapshots, excessive depth, entry counts or
byte sizes are refused. Each provider spawn restages its selected family's
prepared inputs. ROOT preserves existing auth, replaces fixed settings
atomically, and accepts repeated snapshot files only when their bytes match.
Checkout content is staged only by initial preparation.

Provider arguments map the mirror checkout/home/cache/broker paths to
`/workspace`, `/home/doxa`, `/work-cache`, `/run/doxa/session`, preserving path
prefix boundaries. Cwd must be within the mirror checkout. The broker supplies
fixed `CODEX_HOME` and `CLAUDE_CONFIG_DIR`; explicit caller overrides must map
to those fixed homes. Only policy-declared environment names are forwarded.
Values use process environment and socket payloads, and known API credential
values in argv are refused. An explicit `env_remove` suppresses a name;
otherwise allowed values can come from the calling process environment.
Loader/interpreter variables are cleared. Workspace transport accepts Git
commands only; ROOT independently applies its command/option whitelist and
trusted Git configuration overrides. Transport normalization removes only the
exact existing caller settings `core.quotepath=false`, `core.fsmonitor=false`,
`core.hooksPath=/dev/null` and `core.attributesFile=/dev/null`, which ROOT
supplies itself. One literal `--no-pager` prefix is preserved. Other global
options, arbitrary configuration and user aliases are refused.

Snapshots return the crate's `ProviderSnapshot`: bounded bytes plus metadata
for the actual Engine-owned source. The Python client creates a previously
absent mode0600 file inside the caller's random0700 temporary directory using
`O_EXCL`. Rust opens it without following links, checks owner/mode/single-link
identity, and verifies its bounded bytes against the chunk hash. Temporary
files use canonical configured `TMPDIR` on real disk. When it is absent or
empty, they use the session's caller-private `cache/tmp`, created without
following links as mode0700. Verification loads the fixed caller manifest for
the snapshot session and checks its ROOT policy scope before using that
fallback. `/tmp` is refused. A caller-private ancestor may protect subordinate owner-group cache
directories, but the final private directory/file remains0700/0600.

The proof binds session, family, relative path, optional thread, container,
policy hash, boot, device/inode, times, source size/full hash, offset, returned
byte count and chunk hash. Full sources are at most32MiB; nonzero-offset
windows are at most8MiB. Top-level `size` and `sha256` copy the nested source
facts for caller review APIs; they must match the source. Verification removes
only these two aliases, preserving the exact original ROOT metadata. An
unspecified requested thread accepts ROOT's observed thread; a requested
thread must match it. `verify_snapshot` first rejects local byte/scope
tampering, then asks ROOT to re-open and re-hash the original source against
the prior metadata. A caller's review copy does not become source authority.
The caller must invoke this recheck after review before acting on the result.

Focused unit tests inspect command and metadata boundaries and reject forged
caller-owned artifacts. They do not establish live containment or a root-peer
proof. Installed-host evidence belongs to the composed disposable-guest
broker/Engine/native authority suite.
