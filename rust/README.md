# DOXA Rust 2.0

The [1.19 parity tracker](../docs/rust-1.19-parity.md) names the remaining
features before a stable Rust 2.0 release.

Rust 2.0 is the main DOXA frontend. The installer exposes it as `doxa`; the
build artifact is still named `doxa-rs` for source builds. The Python 1.x
frontend is retired from the current installer. Python remains in a private
environment for the LORE and Claude SDK sidecars.

The preview has a native daemon for Codex and vendor chat, plus a Python
Claude SDK sidecar. Its Ratatui frontend attaches to native or Python v1
daemons, draws grouped sessions and split panes, accepts prompts, and renders
Markdown. LORE remains the external authority for memory and secret scrubbing;
the Rust process does not reimplement its store.

Build the native frontend and daemon from this repository:

```sh
cargo build --locked
./task build
./task doctor
./task new
```

Run `cargo build --locked` at the repository root. It uses the shared root
`Cargo.lock` and writes binaries to `target/debug`; `./task` uses
`target/rust-task` so its launcher builds stay separate.
Plain `cargo build` works there too; `--locked` checks the committed lockfile.

`./task` is the repository-local launcher. `run` opens or creates a session,
`new` always creates one, and `doctor` checks launcher dependencies. These
commands build incrementally before running and accept the corresponding
`doxa-rs` options, such as `./task new --engine claude --model NAME`.
`./task build --release` and `DOXA_TASK_PROFILE=release ./task run` select a
release build. `./task test` runs all Rust crate and Claude sidecar tests;
`./task clean` removes only its build directory (`target/rust-task` by default).
The script selects `.venv/bin/python` when present, then `python3`, for LORE
and Claude. Set `DOXA_LORE_PYTHON=/absolute/path/to/python` or pass
`--lore-python` / `--claude-python` to select another interpreter.
`./task install` builds committed `HEAD` and installs `doxa` with its Rust
frontend, daemon, Claude sidecar, and locked Python sidecar environment under
`DOXA_RUST_BIN_DIR` (default `~/.local/bin`). Working-tree edits must be
committed before `install` will include them.

`doxa` now starts a native Codex session when no live sessions exist in the
current project. `new` always starts one; `attach ID` reattaches, `stop ID`
finalizes a running session, `list` shows all live sessions, and `doctor`
checks executable resolution and registry access. `--session ID` and `--socket`
remain available. A unique session ID prefix works for `attach` and `stop`.
The daemon executable is located beside `doxa-rs`, then on `PATH`; an absolute
`DOXA_DAEMON_BIN` overrides that search. The native Codex host needs the
`codex` and `python3` executables on `PATH`, or explicit `--codex-bin` and
`--lore-python` paths. The Python interpreter needs DOXA and LORE installed.
`--model` overrides `DOXA_MODEL` and the Codex entry under `[models]` in
`$DOXA_HOME/config.toml`; `--linger` overrides `DOXA_LINGER_SECS` and the
`linger_secs` config value. `--sandbox` sets the native Codex sandbox. Use
`--engine fixture` only for local integration checks.

Native sessions started in a Git checkout now get a linked worktree under
`$DOXA_HOME/worktrees/<repo>-<session-prefix>` on a `doxa/<session-prefix>`
branch. A sidecar in `worktrees/.meta` records the original repository and
base branch and pins its starting commit for stable diff comparisons. The new
checkout starts at that branch tip; uncommitted changes
in the launch directory stay there. `DOXA_WORKTREE=0` or `worktree_per_session = false` in
`$DOXA_HOME/config.toml` runs in the launch directory; outside Git, sessions
also run there. In a supported Git checkout, failure to create or verify the
managed worktree stops session launch and reports an error. Detaching keeps the
worktree. When the daemon actually exits,
it removes only a verified clean checkout whose recorded branch has no commits
ahead of its base. Dirty trees, unique commits, branch switches, unreadable
metadata, and failed Git checks are kept for manual review. `doxa doctor`
lists verified managed worktrees with no live session and never deletes them.
`doxa worktrees list` previews managed worktrees without an attachable session.
`doxa worktrees cleanup FULL_SESSION_ID --confirm` removes one eligible clean
orphan after rechecking its branch, pinned base, Git status, live session
registry, and advisory lock. It never removes dirty or uniquely committed
work, and it requires the full ID printed by `list`. Legacy Python 1.19
sidecars lack the pinned base and shared lock, so they remain survey only.
Fixture sessions keep their supplied directory unless `DOXA_WORKTREE=1` is
set explicitly for integration testing.
`doxa new --branch NAME` starts the managed worktree from an existing local or
remote-tracking branch. It prefers a same-named local branch over
`origin/NAME`. An unknown branch, disabled worktrees, or a request combined
with `--resume` fails before starting the session; the launch checkout is
never switched. `doxa branch` lists local branch bases from this checkout.
`doxa branch NAME --session ID` changes an idle managed session's base, as
does `/branch` in its pane. The worktree must be clean, have no unique commits,
and still match its pinned base commit. An active or queued turn blocks the
switch. `doxa branch NAME` without a session ID lists available bases.

Or compile and install the main line with the POSIX installer:

```sh
curl -fsSL https://raw.githubusercontent.com/docwilde/doxa/main/scripts/install.sh | sh
```

The default ref is `main`; append a tag or commit SHA after `sh -s --` to pin
another release. This needs Git, Cargo, Python 3.11+, and `uv`. It installs the
Rust `doxa` launcher, `doxa-rs`, `doxa-daemon-rs`, and Claude sidecar in
`~/.local/bin` (override with `DOXA_RUST_BIN_DIR`). It provisions a locked
Python sidecar environment under `DOXA_HOME` and selects it automatically from
any working directory. The native daemon can also attach to compatible Python
1.x sessions.
On Linux, it writes `doxa.desktop` and PNG/SVG icons under `$XDG_DATA_HOME`
(default `~/.local/share`) so DOXA appears in application menus. The shortcut
points to the installed Rust launcher by absolute path and is updated on
reinstall. Set `DOXA_NO_LAUNCHER=1` to skip this step.

Use `doxa help` for CLI commands and options. `doxa update` runs the bundled
installer against `main` and replaces an installed Rust launcher after a
successful build. It uses the current install directory and honors
`DOXA_RUST_REPO_URL` for fork installations. A source build uses `./task install`.

With one live daemon session, `doxa` attaches to it directly. With multiple
sessions, use `--list` and select one by full ID or unique ID prefix.
`--socket` remains available for an explicit path. `--demo` opens the shell
without a connection. `Ctrl+P` opens the action menu; use Up/Down, Enter, and
Esc to navigate the peer map, tool activity, session rail selection, tabs, and
panes. `Shift+Tab` switches panes, including when the terminal reports it as
`BackTab`. `Ctrl+M` and `Ctrl+T` remain direct shortcuts. `Ctrl+Q` detaches the Rust UI
without stopping its daemon; `Ctrl+W` detaches only the active tab and leaves
its session running. `Ctrl+C` is left available for terminal copy.
Bare `/help`, `/about`, `/sessions`, `/model`, `/engine`, `/mode`, `/beliefs`,
`/diff`, `/peers`, `/split`, `/vsplit`, `/pane`, `/movepane`, `/sidebar`, `/dir`, and `/detach` are
handled locally from the prompt. Forms with arguments remain in the draft with
an explicit notice until their Rust behavior is implemented, except `/pane 1|2`,
`/movepane [1|2]`, and `/sidebar on|off|wider|narrower|width N`, which are available. Known DOXA
commands that need more porting, including `/compact`, stay in the draft with a
notice. Unknown provider and plugin slash commands go to the active engine.
`Ctrl+M` opens the read-only peer communications
map; Up/Down selects a peer, R refreshes the live roster, and Esc closes it.
For Python 1.19 fleet runs, `doxa fleet runs` lists manifests under
`$DOXA_HOME/fleet`, `doxa fleet status RUN_ID` shows run and slot phases,
and `doxa fleet attach RUN_ID SLOT` attaches to one slot's live daemon.
`doxa fleet start <doxa-fleet options>` currently invokes the installed
Python fleet harness in the sidecar environment. Its own parser enforces the
capacity, spend budget, socket length, arm barrier, approval, and teardown
rules. `--dry-run` shows assignments without starting sessions.
`doxa fleet preflight --sessions N --run-budget USD [--root PATH]` checks
the memory estimate, explicit spend ceiling, and Unix socket path length in
Rust without creating a run. Use `--allow-unbudgeted` to record an intentional
absence of a ceiling and `--force` only to override the memory estimate.
`--sessions` counts every daemon, including a supervisor when present.
This preview does not verify provider pricing or approval behavior; the live
start still uses the Python harness for those controls.
The native daemon captures `DOXA_SESSION_BUDGET_USD` at startup for Claude
sessions and refuses the next turn once reported USD spend reaches the ceiling.
If a completed budgeted turn has no valid cost, further turns are refused.
Budgeted native Codex and vendor sessions are rejected at startup until their
priced token accounting is implemented. A truthy `DOXA_PEER_INBOUND_TURNS`
lets validated direct peer messages start or queue a turn in native Codex and
vendor sessions. They use the same eight-slot queue as typed prompts;
broadcasts stay passive. Messages that cannot enter the queue are retained
for a later turn up to eight pending frames, with an explicit overflow event.
Claude continues to use its Python sidecar peer loop and rejects this native
switch. Python fleet start remains the live path for full run supervision.
`doxa fleet stop RUN_ID` sends stop requests to the run's validated live
slot sockets and waits up to 60 seconds for each daemon connection to close.
It reports slots with missing sockets or unconfirmed shutdown separately; it
does not signal processes or rewrite the Python supervisor's manifest. A
Python supervisor may continue until its own run loop notices the closed slots.
Use `--root ABSOLUTE_PATH` when the fleet was started under another root.
Run IDs may be unique prefixes. Attachment requires an owner-private manifest
and socket inside the run's private runtime directory.
The map uses `tui-nodes` 0.9 with the Rust frontend's Ratatui 0.29.
Lines show observed traffic and the detail row names sent and received counts.
Type `/peers` or `/mesh` in a session prompt to open the same map. Type
`/msg <session-prefix> <text>` to send a direct message through that session's
daemon. The daemon resolves the prefix among live same-project peers and
scrubs the message with LORE before delivery. The terminal reports failed and
unconfirmed sends; check with the peer before retrying an unconfirmed send.
Native daemons report a same-project, scrubbed peer roster when LORE is
available; other daemons can report an unavailable state. On attach, the frontend restores prompts and
assistant text from the daemon's persisted JSONL file, then follows live
events from the same snapshot boundary. The visible view is limited to the
latest 40 turns, 20,000 assistant characters per turn, an 8 MiB file tail,
and the UI's 512 KiB transcript buffer. It marks omitted earlier content;
the JSONL file retains the full history. Older daemons without snapshot
metadata fall back to their 512-event replay ring. A turn still running at
attach can have text that was streamed but not yet persisted, so its earlier
in-flight deltas may be absent. `Ctrl+T` opens bounded tool activity cards.
The binary version is `2.0.0-alpha.20`;
this is an alpha release.

In the transcript, user messages have a highlighted body and a left rule;
assistant replies keep the normal Markdown surface. Visible `You` and
`Assistant` headings are omitted. Hover an HTTP(S) link to get a pointer cursor
in terminals that support OSC 22; `Ctrl`+left click opens it with the system
browser. Other schemes are never opened. Each turn's tool activity
starts collapsed into one section. Focus the transcript with `Tab`, select a
section with `[` or `]`, and press `Enter` to expand it, or click the section.
Codex and Claude tool results carry scrubbed detail in chunks; the short row
remains a summary, while the expanded section shows up to 256 KiB per result
and marks larger results explicitly. Restored Claude and newly persisted Codex
tool records retain expandable detail within the bounded transcript snapshot.
Older Codex transcripts retain only the assistant answer. During DeepSeek and GLM SSE responses and
Claude thinking streams, `Reasoning/Thinking` shows an approximate live token
count. Its text becomes available only after the completed stream is scrubbed;
Codex reasoning summaries fold into the same row. Select the row
and press `Enter` or click to expand it. The processing spinner appears below
the transcript while a turn is running.
Typing `/` at the start of the prompt shows matching DOXA commands above the
prompt. Use Up/Down or the mouse to choose one and Tab to complete it. The
completed command runs only when you press Enter; unknown provider and plugin
slash commands continue through the normal prompt path.
Expanded sections show bounded live tool inputs and results from the daemon
event stream. `Ctrl+T`
remains the separate tool activity card view.

`Ctrl+R` opens a searchable picker for attached and archived sessions with
bounded transcript tails. Archived transcripts open read-only and never receive
prompts. `/search TEXT` scans a bounded older archived tail. `/resume [ID or
prefix]` opens a picker that can attach a live session or start a saved Claude,
Codex, or vendor session when its engine, model, and stored history can be
verified. If a managed checkout was deleted, DOXA can recreate it from its
retained session branch when the owned sidecar, pinned base commit, and Git
registration all agree. A plain deleted directory or uncertain metadata is
refused; uncommitted files from a deleted checkout cannot be restored. `/queue` opens a picker
above the active prompt for a live session. Its previews are scrubbed by LORE;
press `X` to cancel the selected waiting prompt by its stable ID, `R` to
refresh, or `Esc` to close it. `F2` (or `Alt+G`) opens a 256 KiB worktree diff in an
asynchronous modal. It compares tracked changes with the recorded worktree
base when one exists, or with `HEAD`, and lists bounded untracked filenames
without reading their contents. `F4` keeps that diff visible beside the active
session while its prompt stays usable; `F5` refreshes it and `Alt+PageUp` /
`Alt+PageDown` scroll it. The other session pane reappears when the diff pane
closes. In the modal, `X` selects a regular tracked text hunk at the scroll position for rejection;
in the side pane use `Alt+R`. Type an optional reason (up to 1024 bytes),
then press `Enter` to confirm or `Esc` to cancel. A rejection chosen during an
active turn is visibly queued and does not touch the worktree until the session
is idle. The frontend checks that the same patch still exists, reverse-applies just that hunk, then sends feedback through
the session's normal prompt path. Hunks with rename, copy, creation, deletion,
or mode metadata are excluded because reversing them can change the whole file.
If the hunk has changed, it leaves the file
alone and asks for a refresh. A file with staged changes must be unstaged
before a hunk can be rejected, so the rejected edit cannot remain in Git's
index. Duplicate queued hunks are refused. Pending
rejections block closing the diff or leaving the UI until they finish, and are
cancelled if the session ends. The diff pane requires enough
terminal space for two panes. Each split pane has its own prompt and keeps a draft for its active
session. Its chip row shows engine, model, reasoning effort, repository, context, curated memory, beliefs,
and available billing data. Dollar cost appears only for API-billed sessions. A connected
Claude subscription shows its reported plan and cached quota when the local
CLI cache belongs to the same account; `~` marks stale cached usage. Codex
plan and quota remain unknown until its daemon has a verified provider source,
so no subscription pill is shown for it yet.
The `p X%/u Y%` chip shows curated project and user memory fill
against each scope's cap. A folder outside Git uses `f` in place of `p`.
Both counts and caps come from LORE; `?` means LORE could not report usage.
Click the memory chip to inspect the scoped curated entries and up to 20
current global beliefs in a scrollable menu above the prompt. The beliefs are
global, while project memory follows the main repository when the session runs
in a worktree. The separate beliefs picker can page beyond the first 20.
The repository chip shows the base branch, checked-out worktree branch, and
commit for the active session. A plain folder shows `dir NAME`. Hover over a
chip for its meaning, or click a read-only chip to see details above the
prompt. Estimated cost is labeled. In a prompt, `Enter` submits, while
`Shift+Enter` or `Alt+Enter` inserts a newline (`Ctrl+J` also works when
reported distinctly by the terminal). An ambiguous `Ctrl+Enter` report never
submits a prompt; use `Alt+Enter` for a newline. `Alt+Up` restores a rejected
draft when one exists, and otherwise resizes the split. Arrow keys move the cursor across
lines; `Home`, `End`, `Backspace`, and `Delete` edit at the cursor. Bracketed
paste preserves line breaks, removes terminal control characters, and never
submits. Prompts are capped at 10 KiB; an oversized paste is truncated with a
notice. Drafts and cursor positions stay with each session pane.
Permission, engine, model, effort, repository, and LORE chips share one row directly above
each pane's prompt. Their pickers, the action menu, and daemon question choices
expand upward in the active pane, leaving its prompt and the other pane visible.
`Alt+E` opens an engine picker,
then a model, reasoning effort, and first-prompt form that starts a new session
in the selected pane. DeepSeek and GLM model choices follow the selected
vendor. With an API key, DOXA refreshes that vendor's bounded model catalog
without blocking redraw. DeepSeek's reported per-model effort levels drive
the form; known GLM models use measured effort levels because its catalog
does not expose a verified effort contract. If the lookup fails, the form
labels its static fallback. A live catalog with no supported model/effort
pair leaves launch disabled with an explanation. A blank model for other
engines uses the configured default. The effort chip immediately follows the
model chip and displays the active daemon's reported value, or `?` when it
has no verified value. Its tooltip identifies the level as effort. `Alt+F`
and bare `/effort` open an inline picker for the current DeepSeek or GLM
session for known models. The daemon accepts a
change only while the session is idle with no queued prompts; a successful
change applies to the next admitted turn and updates the chip after its
event. Newer models discovered only from a live catalog remain available
in the new-session form, but live effort control is unavailable until their
model capability is built into the native host. Claude uses the sidecar
installed beside `doxa-rs`; `DOXA_CLAUDE_SCRIPT` can select another absolute
path during development. The active
session's engine cannot be switched. `Alt+M` opens the live model picker when the daemon
advertises model control. Claude catalog choices come from a bounded startup
CLI probe; an unavailable catalog offers no guessed models. The colors follow
Python DOXA's warm dark palette.
`Alt+P` opens the Claude permission mode picker when supported by the session.
Entering `dontAsk` requires a second Enter confirmation because unapproved
calls are silently denied.
`Alt+L` opens the LORE picker when the external bridge is available. It shows
bounded recent beliefs, a search hit, and evidence for the selected belief.
Select a belief to review its complete subject and claim. LORE refuses an
action if its scrubber would hide any part of the claim. After
reading to the end, choose `C` to confirm, `X` to contradict, `S` to mark
stale, or `R` to retract. Enter a note and press Enter to apply; retract also
requires a separate `Y` confirmation. LORE verifies the original claim,
scope, and active status under its write lock. A changed belief is refused;
inspect LORE before retrying if a transport error leaves the result unknown.
Older LORE builds without reviewed belief actions keep this picker read only.
Press `P` with an empty search to page staged proposals, then Enter to scroll
the complete raw proposal. After reading to the end, `A` or `R` arms one
approval or rejection; Enter confirms it. The sidecar rechecks the displayed
SHA-256 and inode before asking LORE to claim that one proposal. Older LORE
builds without the atomic API keep proposal review read only. An approval that
lands but fails to archive is reported as a partial completion and is never
retried automatically. The local `/pending` command opens the proposal picker
without sending a prompt to the model. `Alt+X` asks for confirmation before
stopping the active session; the session stays visible as read-only after a
successful stop.
The local `/attach QUERY` command searches live daemon IDs and titles, then
rechecks the selected ID before connecting and opening it in a new tab. An
exact ID takes precedence over an ID prefix, then a title match. If that
session is already open, the command focuses its tab. Bare `/attach` opens a
filtered picker above the active prompt when several detached sessions are
available; keyboard and mouse selection keep the prompt visible. `/rename NAME` pins the active
tab's label in the shared tabset; bare `/rename` restores its automatic label.
Unimplemented slash commands remain in the draft with a visible error and
are never submitted as model prompts.
The diff view supports file and hunk navigation with `N`/`P` and `J`/`K`
in its modal, or `Alt+N`/`Alt+B` and `Alt+J`/`Alt+K` in the persistent pane.

Alpha tags identify preview snapshots. A stable 2.0 release waits until the
frontend reaches feature parity and passes end-to-end terminal and daemon
tests.

Rust CI runs on `main`, including the Python sidecar and compatibility tests
needed by the current Rust runtime. Broader parity and release tests remain
before a stable 2.0 release.

Start the native Claude host from an installed preview with
`doxa new --engine claude --claude-python /absolute/path/to/python`.
For a source build, pass `--claude-script /absolute/path/to/claude_sidecar.py`
or set `DOXA_CLAUDE_SCRIPT` to that absolute path. The Python interpreter
must have DOXA, LORE, and the Claude Agent SDK installed. Add `--model NAME`
to choose a model, or `--resume SESSION_ID` to resume that session's Claude
conversation. `doxa doctor --engine claude` checks the selected Python
interpreter, sidecar script, daemon, and runtime path.
Native Claude sessions can change their model and permission mode through
capability-gated live controls. The daemon reports the selected values to
attached clients. The Rust launcher has no explicit bypass arming flow yet,
so `bypassPermissions` is refused. Entering `dontAsk` requires an idle session
with no queued prompts.

Start native DeepSeek or GLM plain chat with `doxa new --engine deepseek`
or `doxa new --engine glm`. Set `DEEPSEEK_API_KEY` or `ZAI_API_KEY` in the
environment, respectively. `--lore-python` selects the Python interpreter
used for LORE scrubbing (default `python3`); it must have DOXA and LORE
installed. `--model` overrides the matching `deepseek` or `glm` entry under
`[models]` in `$DOXA_HOME/config.toml`. The Codex-only `DOXA_MODEL` setting
does not select a vendor model. `--effort low|high|max` is optional; DeepSeek
also accepts `none`. `doxa doctor --engine deepseek|glm` checks the daemon,
LORE interpreter, provider key presence, and effort value without printing the
key. Use `doxa new --engine deepseek|glm --resume SESSION_ID` to resume an
existing vendor session with saved messages. Pass the exact full session ID;
the vendor and resolved model must match the saved state. Native vendor tools
are disabled by default. Set `DOXA_VENDOR_TOOLS=workspace-read` when launching
the session to allow the model to read UTF-8 files below the workspace. File
contents are sent to the model provider after LORE scrubbing. This is a
session-wide opt-in; there is no per-call approval in this preview.
With a DeepSeek API key, an optional background request to DeepSeek's official
balance endpoint can add a balance chip. USD and CNY balances are shown
separately without conversion. An unavailable response leaves the chip hidden;
GLM/Z.ai does not make a balance request.

## Native daemon

`doxa-daemon` is a native protocol v1 host process for lifecycle and transport
integration. Run `cargo build --manifest-path rust/doxa-daemon/Cargo.toml`, then
`rust/doxa-daemon/target/debug/doxa-daemon --cwd /path/to/project`. The process
prints no prompt or transcript data. Its Unix socket and owner-private registry
entry live under `DOXA_RUNTIME_DIR` when set, otherwise `$XDG_RUNTIME_DIR/doxa`
or `~/.local/share/doxa`. The registry's `daemon_socket` field can be passed to
`doxa-rs --socket` or used by Python `EngineClient`; it has the same hello,
attach, prompt, event, reply, and stop wire shapes as the Python daemon. `--linger`
sets seconds to wait after the last client detaches (default 120). An unclaimed
process gets at least 120 seconds for its first attach. SIGTERM and SIGINT stop
the socket and remove the registry entry.

The default `fixture` host returns one fixed response for every prompt. To
select the first native Codex host explicitly, pass `--engine codex` with
`--codex-bin /absolute/path/to/codex` and
`--lore-python /absolute/path/to/python`. The interpreter must have DOXA and
LORE installed. The Codex CLI must already be authenticated. Both executable
paths are resolved and checked before opening a socket. Optional `--model`
and `--sandbox read-only|workspace-write|danger-full-access` are passed as
separate CLI arguments, never through a shell. A missing LORE sidecar prevents
the session from starting; a scrub failure during a turn withholds further
provider events and fails the turn. The Python sidecar is a temporary
dependency while Rust memory integration is built.

To host Claude, pass `--engine claude --claude-python /absolute/path/to/python
--claude-script /absolute/path/to/claude_sidecar.py`. This uses DOXA's Python
`SessionEngine` and Claude Agent SDK in a separate process. `--model` is optional.
To resume, also pass `--session-id ID --resume true`. The daemon forwards bounded
events and supports `answer_needs_input`, `interrupt`, and graceful `finalize`
on exit. The Python SDK remains required in this alpha.

The native plain-chat vendor host accepts `--engine deepseek` or `--engine glm`
with `--lore-python /absolute/path/to/python`; the interpreter must have DOXA
and LORE installed. Keys come only from `DEEPSEEK_API_KEY` or `ZAI_API_KEY` in
the environment. `--model` and `--effort low|high|max` are optional; DeepSeek
also accepts `--effort none`. Provider endpoints are fixed in production.
By default the host advertises no tools and rejects provider tool calls. With
`DOXA_VENDOR_TOOLS=workspace-read`, it advertises one read-only tool. It accepts
relative paths only, rejects hidden path components and symlinks, and reads at
most 64 KiB from a regular UTF-8 file. It cannot write files, run commands, or
call LORE and peer operators. Tool exchanges are kept only in the active turn;
saved history and transcripts contain the prompt and final response. The tool
does not provide file access controls within the workspace, so enable it only
when workspace files may be sent to the vendor. The host persists
bounded plain-chat history beside the Python transcript as
`<session-id>.messages.json`. Restart with the same `--session-id ID --resume
true`; missing, corrupt, wrong-engine, or wrong-model state refuses resume.
Python's engine/messages envelope is readable; newly written envelopes also
record session ID and model. LORE scrubs every saved string before an atomic
replacement. Accepted turns also append Python 1.19-shaped user and assistant
records to `<session-id>.jsonl`. Resume verifies that JSONL and the replay
file contain the same turns; a partial two-file write refuses further turns
and later resume. The full provider response is withheld until LORE scrubs it;
a scrub or storage failure fails the turn and commits no history.
It reports provider model and token usage but no dollar cost. Interrupt and
stop cancel the in-flight HTTP request. Native hello exposes an owner-checked
JSONL path and byte boundary so the TUI can restore completed vendor and Codex
turns before attaching to the live event ring. LORE context, tool execution,
pricing, and live-provider validation remain open.

Codex turns use `codex exec --json` and resume subsequent turns using the
provider thread ID. User and assistant text is appended to Python 1.19-shaped
JSONL under LORE's project directory, and `<session-id>.codex.json` records the
provider thread for a later daemon started with the same `--session-id`.
The LORE sidecar supplies the exact project identity and scrubs every persisted
string; failure to scrub leaves the new record unwritten. An existing transcript
without a thread ID refuses a fresh Codex thread. `interrupt` cancels the
running CLI process group, and `stop` cancels it and closes the daemon. The
native Codex host prepends a LORE snapshot (up to 64 KiB) to the first provider
turn only, under a memory header and footer. The snapshot is absent from the
displayed prompt and transcript, and a resumed provider thread receives no
duplicate. If the snapshot is unavailable or too large, the turn proceeds
without context; LORE scrubbing remains required for visible and persisted
text. Transcript or thread-state write failures withhold a turn or stop the
session. An interrupted or failed Codex turn leaves a durable incomplete marker
and refuses unsafe thread resume after restart. After each native Codex turn
and at shutdown, the external LORE sidecar
incrementally indexes its verified transcript descriptor when LORE 0.58.4 or
newer exposes `index_live_fd`. Older installed LORE plugins still provide
scrubbing and snapshots but cannot advertise transcript indexing. Automatic Codex proposal
review remains unavailable, matching the Python Codex host; MCP registration
is still open. The registry reports the selected engine. `status`, `interrupt`,
and `stop` are supported; Claude also supports `answer_needs_input`,
`set_model`, and `set_permission_mode`; native vendor hosts also support
`set_effort` while idle. `peers` returns a
read-only, same-project roster of live peer IDs and LORE-scrubbed titles (up
to 32). It fails closed when the LORE scrubber is unavailable. Other calls
return an explicit error. The
`daemon_socket` is suitable for local TUI or Python `EngineClient` attach.
`socket_path` identifies the private peer inbox; `msg` sends scoped local peer
messages. Inbound messages are surfaced as peer events. Remote peer routing and
automatic turn handling remain open. Treat this as an integration alpha.

## Transcript persistence crate

`doxa-transcript` is a standalone Rust crate for Python 1.19 session JSONL,
Codex `<session-id>.codex.json` records, and vendor `.messages.json` replay.
It reads a bounded 8 MiB/20,000-line
tail, appends original JSON objects with the Python `engine` override, and
retains unknown keys. Writes require a caller-supplied secret scrubber that
visits every string value. Codex metadata updates merge existing keys and use
an atomic replacement. Files and the project directory must belong to the
current user; symlink and hard-link file targets are refused.

Run `cargo test --locked --manifest-path rust/doxa-transcript/Cargo.toml`.
The native Codex host uses this crate. The Rust UI still relies on its existing
transcript reader.
