# Sandboxed sessions, by default, on top of worktrees — specification

Status: **draft for review**. Nothing implemented.

## What worktrees already do, and what they do not

Since v0.17.0 every session runs in its own git worktree on `doxa/<session-id>`
(`doxa/worktrees.py`, sidecar recording `main_root`, `branch`, `base_ref`). That
isolates **what the agent can change in git terms**: a branch of its own, a tree
of its own, a base to diff against, a `finalize` that refuses to discard commits
it cannot account for.

It isolates nothing about **what the process can reach**. A worktree is an
ordinary directory owned by the ordinary user, and the CLI DOXA spawns inherits
that user's whole reach:

- `$HOME` in full — `~/.ssh`, `~/.aws`, `~/.config`, browser profiles, the
  `~/.claude/.credentials.json` that `cli_isolation` deliberately *copies* into
  `~/.doxa/claude-cli/` precisely because it is a credential
- **every other repo on the machine**, including the main checkout the worktree
  was cut from, and every *sibling* worktree — that is, every other live DOXA
  session's tree
- **DOXA's own durable state** under `$DOXA_HOME` (default `~/.doxa`): the
  worktree sidecars, the isolated CLI config, the peer registry
- **the network**, without qualification
- **the peer daemon's `AF_UNIX` socket**, whose `chmod 0600` stops other *users*
  and does nothing about a process running as this one

So `cd ..` reaches the main repo. `git -C ~/other-repo push` works. A dependency
install with a postinstall script runs as the user who can read the SSH key. None
of that is a defect in the worktree design; it is the boundary the worktree
design was never drawn to hold.

**The proposal: worktrees keep saying what a session may change; a sandbox says
what it may reach. Both on, by default, per session.**

## The mechanism, measured rather than assumed

Probed on this machine (Linux 7.0.0-28, `claude` 2.1.228 at
`~/.local/share/claude/versions/2.1.228`):

| fact | measurement |
|---|---|
| the CLI implements a Linux sandbox | 33 `bwrap` / 17 `bubblewrap` / 83 `seccomp` markers in the bundle, plus `[Sandbox Linux] Wrapped command with bwrap (` |
| it shells out to bubblewrap | `bwrapPath` setting, `bubblewrap (bwrap) not installed`, `apt install bubblewrap` |
| macOS takes a different path | `sandbox-exec` |
| bubblewrap is present here | `/usr/bin/bwrap` |
| unprivileged user namespaces are allowed | `kernel.unprivileged_userns_clone = 1` |
| the kernel has what it needs | `CONFIG_SECCOMP=y`, `CONFIG_SECURITY_LANDLOCK=y` |
| **`socat` is missing here** | `command -v socat` → nothing; the bundle's own remedy line is `install missing tools (e.g. apt install bubblewrap socat)` |
| unix-socket blocking is seccomp, not a mount trick | `[Sandbox Linux] Applying seccomp filter for Unix socket blocking` |
| deny paths fail closed | `[Sandbox Linux] Deny path could not be resolved through symlinks, failing closed:` |

The last two matter to us specifically and are picked up again below.

## The wiring, which is one field

`ClaudeAgentOptions.sandbox: SandboxSettings | None` (SDK 0.2.144,
`types.py:2252`). The transport merges it into a JSON `--settings` value
(`_internal/transport/subprocess_cli.py:466-513`). DOXA passes **no** `settings`
today, so this is strictly additive to `_build_options` (`doxa/engine.py:1664`)
and lands next to the `env=cli_isolation_mod.spawn_env()` line that already
exists for exactly this reason.

`SandboxSettings` carries `enabled`, `autoAllowBashIfSandboxed`,
`excludedCommands`, `allowUnsandboxedCommands`, `network`, `ignoreViolations`,
`enableWeakerNestedSandbox`.

**The trap is in its own docstring, and it inverts the obvious design:**

> Filesystem and network restrictions are configured via permission rules, not
> via these sandbox settings — Read deny rules, Edit allow/deny rules, WebFetch
> allow/deny rules.

So `sandbox.enabled = True` alone buys process containment with a **default**
policy. The policy that makes it *this session's* sandbox — write only inside
this worktree, never the main root, never a sibling worktree, never
`~/.doxa/claude-cli` — is a set of **permission rules DOXA must synthesize per
session and pass through the same `--settings` JSON**. That is the real work of
this feature; `enabled: True` is the easy half, and shipping only the easy half
would produce a session that *looks* sandboxed and still writes to `~/.ssh`.

## The per-session policy

Derived from the sidecar the worktree already writes, so there is one source of
truth and no second thing to keep in sync:

**Writable:** the worktree path, and nothing else. Not `main_root` — a session
that wants to write the main checkout is asking to leave its branch, which is
what `/branch` and `finalize` are for.

**Readable but not writable:** `main_root` (an agent reading the repo it branched
from is normal and useful), the isolated CLI config dir, the system toolchain.

**Denied outright:** `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, the real
`~/.claude/.credentials.json`, every *sibling* worktree under
`$DOXA_HOME/worktrees`, and the peer registry. Sibling worktrees are the one
entry a reasonable person would forget: two live sessions are two branches of the
same project, adjacent on disk, and "the agent edited the other session's tree"
is a failure with no error message.

**Network:** default-deny with an allowlist. The honest starting allowlist is the
API endpoint the CLI itself needs plus whatever the repo's toolchain genuinely
fetches — and on this machine `socat` is absent, so **network filtering is not
available until it is installed**. That is a supported state, not a blocker, but
it must be *reported* (below), because a sandbox that silently does three of its
four jobs is worse than one that does none.

**`excludedCommands`:** `git` is the live question. The bundle carries
`Git commands outside the original working directory require permission checks
when sandbox is enabled`, which reads as: sandboxed git inside the worktree is
fine, and git aimed elsewhere is the thing being stopped — which is exactly our
policy, so **git should stay sandboxed** rather than be excluded. `docker` is
excluded by necessity if used at all (it is a socket to a daemon that is not
sandboxed, and pretending otherwise is theatre).

## What "by default" has to mean

Default-on is the point of the request, and it has three obligations:

1. **A session that cannot be sandboxed says so, loudly, and still starts.**
   `bwrap` missing, userns disabled, an unprivileged container — the CLI's own
   string for this is `Commands will run WITHOUT sandboxing. Network and
   filesystem restrictions will NOT be enforced.` DOXA must surface that at the
   same volume, in the transcript at boot and in the status bar for the life of
   the session. Silently degrading to unsandboxed is the one outcome this spec
   exists to prevent.
2. **It is visible.** A `sandbox:` chip, built like the permission-mode chip:
   green-ish when the policy is fully applied, amber when partial (containment
   yes, network filtering no — today's state on this machine), red-with-reason
   when off. Same rule as `mode:` — *an option a user can see is an option that
   works*, and its inverse: a protection the user cannot see is one they cannot
   rely on.
3. **The escape hatch is per-session, explicit, and never persisted wider than
   the session.** Same shape the permission-mode work landed on: a stored value
   must not be able to disarm a session opened later in a repo the user has not
   read yet.

## Interaction with permission modes — the part to get right

The two systems answer different questions and it is tempting to collapse them.
`mode:` is *does DOXA ask you before a tool runs*. `sandbox:` is *what can that
tool touch if it does run*. They multiply; neither substitutes.

`autoAllowBashIfSandboxed` (SDK default `True`) is exactly where they meet, and
it is a real improvement — a bash call that provably cannot leave the worktree
or reach the network is not worth a prompt, and prompt fatigue is what turns
`bypassPermissions` on in the first place. **But it must not become a back door
into arming bypass.** The invariant: a sandbox may reduce how often DOXA asks; it
may never change *which modes are reachable*. In particular a sandboxed session
started without `--allow-dangerously-skip-permissions` still cannot reach
`bypassPermissions`, and the chip must not imply otherwise.

The honest phrasing for the transcript line, once this ships: *bash runs inside
this worktree with no network; that is why it stopped asking.*

## Two failure modes worth naming in advance

**The peer socket.** The seccomp filter blocks unix sockets. DOXA's daemon socket
is a unix socket in `~/.doxa`. Sandbox a session naively and peers, detach and
reattach break — as a *silent* loss of a feature, not an error. `allowUnixSockets`
takes the specific path; that entry is load-bearing and needs its own test.

**Symlinks.** `[Sandbox Linux] Deny path could not be resolved through symlinks,
failing closed`. `cli_isolation` **symlinks** `skills/` (measured, v0.10.0:
credentials copied, skills symlinked, 12/12 loaded). A deny rule that resolves
through that symlink and fails closed would take the skills with it. Whether it
does is a measurement, not a guess, and it belongs in the first probe.

## Testing bar

- a sandboxed session cannot write outside its worktree — asserted against a real
  spawned CLI, not a fake, since the whole feature lives in the child process
- it cannot write the main checkout, and *can* read it
- it cannot read a sibling worktree, and the test creates a real second one
- it cannot read `~/.ssh` — with a real file planted in a `HOME` the test owns
- peers still work: a sandboxed session joins the registry and exchanges a frame
- the 12/12 skills load measurement from v0.10.0 still holds under the sandbox
- with `bwrap` made unavailable, the session **starts**, the chip is red, and the
  transcript says what is not enforced
- with `socat` unavailable (today's real state) the chip is amber and names
  network filtering as the missing half
- the mode/sandbox invariant: an unarmed session is still unable to reach
  `bypassPermissions` with the sandbox on
- `/about` reports the sandbox mechanism and the policy actually applied, since
  that is the screen a bug report is copied from

## Open questions

1. **Does the allowlist come from the repo?** A `.doxa/sandbox.toml` in the
   project would let a repo declare the domains its toolchain needs — and would
   be a file the agent can write, which makes it a policy the agent can widen.
   That is probably fine (it is committed and reviewable) and probably needs
   saying out loud.
2. **What happens to `!` shell commands?** The v0.24.0 `!` prompt runs *DOXA's*
   shell, not the agent's. The user typing a command is not the threat model, so
   the reading is that `!` stays unsandboxed — but it should be *stated*, because
   a user who sees `sandbox: on` may reasonably assume it covers everything in
   the window.
3. **Per-repo default, or global?** Sandboxing a scratch repo and sandboxing a
   client's repo are the same act with different stakes. A global default with a
   per-repo override is the smaller design; a per-repo default is the one people
   actually want after the first week.
4. **`enableWeakerNestedSandbox`** exists for unprivileged Docker and *reduces
   security* by name. If DOXA ever runs inside a container, does it opt in
   silently, or refuse and report? The precedent set above says report.
5. **Does the sandbox change what `finalize` can conclude?** It reasons about
   commits in the worktree; sandboxed git inside the worktree should be
   unaffected. Should, and therefore must be measured before default-on.
