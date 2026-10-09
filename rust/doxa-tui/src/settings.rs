//! Native settings catalog and persistence. Environment overrides
//! are explicit, read-only rows; writes preserve every unrelated TOML key.
use std::{io, path::Path};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind { Text, Number, Bool, BoolOn, Choice, Format, Key }
#[derive(Debug, Clone, Copy)]
pub struct Setting {
    pub key: &'static str, pub env: &'static str, pub label: &'static str,
    pub category: &'static str, pub kind: Kind, pub choices: &'static [&'static str],
    pub default: &'static str, pub read_only: bool, pub help: &'static str, pub note: &'static str,
}
pub const CATEGORIES: &[&str] = &["Session", "Fleet", "Memory", "Appearance", "Keys", "Notifications", "Remote", "Paths", "About"];
macro_rules! key_setting {
    ($key:literal, $label:literal, $default:literal) => {
        Setting { key: $key, env: "", label: $label, category: "Keys", kind: Kind::Key,
            choices: &[], default: $default, read_only: false,
            help: "Window shortcut: Ctrl, Alt and Shift modifiers plus a letter, arrow, Delete, Tab, comma or F1-F12; 'none' unbinds it. Takes effect when settings are saved.",
            note: "Editing keys in the prompt and menus remain local to those controls. Duplicate shortcuts are rejected." }
    };
}
pub const SETTINGS: &[Setting] = &[
    Setting { key: "session_isolation", env: "DOXA_SESSION_ISOLATION", label: "new-session isolation", category: "Session", kind: Kind::Choice, choices: &["native", "docker-open", "docker-offline"], default: "native", read_only: false, help: "Default execution boundary for new sessions; the new-session form can override it.", note: "Docker requires a reviewed pinned image and local rootless Engine. Saved sessions retain their recorded policy. No-network Docker blocks online CLI inference; API-vendor HTTP stays on the trusted host." },
    Setting { key: "fleet_alignment_supervisor", env: "DOXA_FLEET_ALIGNMENT_SUPERVISOR", label: "independent supervisor model", category: "Fleet", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Provider:model for independent read-only fleet review. Empty disables it; claude, codex, deepseek or glm.", note: "These stateless calls use API credentials, separate from CLI subscription sessions. Model has no tools or worker conversation history. Review budget is mandatory." },
    Setting { key: "fleet_supervision_mode", env: "DOXA_FLEET_SUPERVISION_MODE", label: "independent supervisor action", category: "Fleet", kind: Kind::Choice, choices: &["shadow", "enforce"], default: "enforce", read_only: false, help: "Shadow records verdicts; enforce pauses delegation for uncertainty, drift or blocked work.", note: "Enforce mode pauses new delegation on reviewer outages. Shadow records outages. Only an explicit human continue clears an enforced pause." },
    Setting { key: "fleet_message_review", env: "DOXA_FLEET_MESSAGE_REVIEW", label: "fleet message review", category: "Fleet", kind: Kind::Choice, choices: &["off", "shadow", "enforce"], default: "off", read_only: false, help: "Fast semantic admission mode for actual fleet peer messages.", note: "Selecting a mode opts scrubbed charter, assignments and messages into the selected external judgment service. Deterministic scope and provenance gates always apply to reviewed fleets." },
    Setting { key: "fleet_message_judge", env: "DOXA_FLEET_MESSAGE_JUDGE", label: "fast message judge model", category: "Fleet", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Choose jev:jev-1.13.0 or llm:provider:model independently of the supervisor.", note: "Jev needs TYPESAFE_API_KEY. LLM providers: claude, codex, deepseek, glm; these require API credentials. Results cannot approve new authority or task changes." },
    Setting { key: "fleet_review_budget", env: "DOXA_FLEET_REVIEW_BUDGET", label: "fleet review budget ($)", category: "Fleet", kind: Kind::Number, choices: &[], default: "", read_only: false, help: "Dollar allocation for all independent supervisor and message judge calls in each new fleet.", note: "Subtracted from the worker budget. Shared conservative token reservations and call ceilings include failed calls. Reserved estimates are not invoices." },
    Setting { key: "fleet_review_input_price", env: "DOXA_FLEET_REVIEW_INPUT_PRICE", label: "review input price ($/Mtok)", category: "Fleet", kind: Kind::Number, choices: &[], default: "100", read_only: false, help: "Conservative owner-approved input rate for the selected LLM reviewer.", note: "Unknown model rates never imply free work. The default is deliberately conservative. Pinned Jev 1.13 uses documented input-only pricing." },
    Setting { key: "fleet_review_output_price", env: "DOXA_FLEET_REVIEW_OUTPUT_PRICE", label: "review output price ($/Mtok)", category: "Fleet", kind: Kind::Number, choices: &[], default: "100", read_only: false, help: "Conservative owner-approved output rate for the selected LLM reviewer.", note: "Every LLM call has a hard 512 output token limit." },
    Setting { key: "fleet_review_threshold", env: "DOXA_FLEET_REVIEW_THRESHOLD", label: "message risk threshold", category: "Fleet", kind: Kind::Number, choices: &[], default: "0.5", read_only: false, help: "Probability threshold for semantic quarantine; review against labeled examples before enforcing.", note: "Each new fleet review shows the exact threshold. Model confidence is evidence, never permission." },
    Setting {key:"docker_image",env:"DOXA_DOCKER_IMAGE",label:"Docker worker image digest",category:"Session",kind:Kind::Text,choices:&[],default:"",read_only:false,help:"Reviewed local image pinned as sha256:CONTENT_ID or NAME@sha256:DIGEST.",note:"DOXA never builds repository Dockerfiles or pulls an unreviewed image when a session starts."},
    Setting {key:"docker_host",env:"DOXA_DOCKER_HOST",label:"local rootless Docker socket",category:"Paths",kind:Kind::Text,choices:&[],default:"",read_only:false,help:"Local unix:///run/user/UID/docker.sock endpoint; default follows the host user.",note:"Remote TCP Engines, the rootful system socket and Docker sockets inside workers are forbidden."},
    Setting {key:"docker_memory_bytes",env:"DOXA_DOCKER_MEMORY_BYTES",label:"Docker memory ceiling (bytes)",category:"Session",kind:Kind::Number,choices:&[],default:"4294967296",read_only:false,help:"Enforced cgroup memory limit; default 4 GiB, configurable for the workload.",note:"Disk usage has a separate monitored soft ceiling, not a hard quota."},
    Setting {key:"docker_cpus",env:"DOXA_DOCKER_CPUS",label:"Docker CPU ceiling",category:"Session",kind:Kind::Number,choices:&[],default:"2",read_only:false,help:"Enforced CPU quota; fractional CPUs are allowed.",note:"Requires effective rootless cgroup v2 delegation."},
    Setting {key:"docker_pids",env:"DOXA_DOCKER_PIDS",label:"Docker process ceiling",category:"Session",kind:Kind::Number,choices:&[],default:"256",read_only:false,help:"Enforced cgroup process count limit.",note:"Session changes do not implicitly change resources or image; these defaults apply to new sessions."},
    Setting {key:"docker_disk_soft_limit_bytes",env:"DOXA_DOCKER_DISK_SOFT_LIMIT_BYTES",label:"Docker session disk soft ceiling (bytes)",category:"Session",kind:Kind::Number,choices:&[],default:"21474836480",read_only:false,help:"Monitored usage ceiling for a new session's checkout, private home, cache and retained migration files; default 20 GiB.",note:"Checked before each new provider turn. A running worker can exceed it before the next check; this is not a hard filesystem quota."},
    Setting {key:"docker_disk_free_floor_bytes",env:"DOXA_DOCKER_DISK_FREE_FLOOR_BYTES",label:"Docker host free-space floor (bytes)",category:"Session",kind:Kind::Number,choices:&[],default:"2147483648",read_only:false,help:"Refuse new Docker sessions and provider turns when available space on their session filesystem falls below this floor; default 2 GiB.",note:"Measured at launch, resume and before each turn. This cannot reserve space against concurrent writers."},
    key_setting!("key_new_tab", "new tab", "Ctrl+T"),
    key_setting!("key_close_tab", "close tab", "Ctrl+W"),
    key_setting!("key_close_tab_alt", "close focused tab", "Delete"),
    key_setting!("key_quit", "quit and detach", "Ctrl+Q"),
    key_setting!("key_previous_tab", "previous tab", "Ctrl+Left"),
    key_setting!("key_next_tab", "next tab", "Ctrl+Right"),
    key_setting!("key_previous_pane", "previous pane prompt", "Shift+Left"),
    key_setting!("key_next_pane", "next pane prompt", "Shift+Right"),
    key_setting!("key_next_pane_alt", "next pane alternate", "Alt+Tab"),
    key_setting!("key_tools", "tool calls", "Alt+T"),
    key_setting!("key_palette", "action palette", "Ctrl+P"),
    key_setting!("key_search", "session search", "Ctrl+R"),
    key_setting!("key_settings", "settings", "Ctrl+,"),
    key_setting!("key_peer_map", "peer map", "Ctrl+M"),
    key_setting!("key_sidebar", "session rail", "F3"),
    key_setting!("key_diff", "diff", "F2"),
    key_setting!("key_diff_alt", "diff alternate", "Alt+G"),
    key_setting!("key_split_horizontal", "stacked split", "Alt+H"),
    key_setting!("key_split_vertical", "side-by-side split", "Alt+V"),
    key_setting!("key_model", "model picker", "Alt+M"),
    key_setting!("key_effort", "effort picker", "Alt+F"),
    key_setting!("key_permission", "permission picker", "Alt+P"),
    key_setting!("key_engine", "engine picker", "Alt+E"),
    key_setting!("key_lore", "LORE beliefs", "Alt+L"),
    key_setting!("key_stop", "stop session", "Ctrl+X"),
    key_setting!("key_delete_transcript", "delete session transcript", "Ctrl+Delete"),
    Setting { key: "engine", env: "DOXA_ENGINE", label: "engine", category: "Session", kind: Kind::Choice, choices: &["", "claude", "codex", "deepseek", "glm"], default: "claude", read_only: false, help: "Which engine drives NEW sessions (doxa.engines -- `doxa --engine <id>` is the flag layer, `/engine` the in-app one)", note: "Not every session surface exists on every engine, and the ones that do not are HIDDEN rather than shown inert -- no permission-mode chip where there are no modes, no ctx chip where no window size is reported, no cost chip where no dollar figure is. `/engine` prints what each one can and cannot do, read off doxa.engines.EngineCapabilities itself rather than described here, where it would go stale. An engine is chosen at CONNECT, so a change here reaches NEW sessions and tabs and never the running one." },
    Setting { key: "model", env: "DOXA_MODEL", label: "model", category: "Session", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Model preference for the active session's engine, used by new sessions of that engine (/model switches the live session). DOXA_MODEL overrides every engine.", note: "" },
    Setting { key: "effort", env: "DOXA_EFFORT", label: "effort", category: "Session", kind: Kind::Choice, choices: &["", "low", "medium", "high", "xhigh", "max"], default: "", read_only: false, help: "Default reasoning effort for new sessions; use the effort chip or /effort for the current session", note: "Supported current-session changes require an idle provider and verified capability. Claude resumes its existing provider conversation with the selected effort; Codex applies it to the next turn." },
    Setting { key: "allow_bypass", env: "DOXA_ALLOW_BYPASS", label: "allow bypass", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Let NEW sessions reach bypassPermissions at all (spawns their CLI with --allow-dangerously-skip-permissions)", note: "OFF by default, and the default is the point. The claude CLI arms this capability at LAUNCH, not at runtime: a session started without the flag cannot enter bypassPermissions, and no setting can retrofit one that is already running. While this is off, the mode is absent from the permission picker, the chip's picker and /mode's list rather than being offered and refused. Turning it on puts every session spawned afterwards one keystroke away from running tools unapproved, in every repository you open." },
    Setting { key: "adopt_plugins", env: "DOXA_ADOPT_PLUGINS", label: "adopt claude plugins", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Load the commands, skills and agents from your OWN installed Claude Code plugins into NEW sessions (doxa.claude_plugins.adopt) -- never their hooks or MCP servers, and never the LORE plugin", note: "OFF by default: isolation (doxa.cli_isolation, item AA) stays the resting posture, and adopting your plugins is a choice you make, not something a fresh install does for you. Turning this on does not undo the isolation fix -- hooks and MCP servers stay refused unconditionally (see docs/plans/plugins.md), only commands/skills/agents from plugins your own ~/.claude/settings.json already has enabled are staged into a sanitized copy and loaded via --plugin-dir, one session-scoped flag per adopted plugin. /plugins previews what this would adopt before you turn it on; /reload-plugins re-scans for NEW sessions without restarting doxa." },
    Setting { key: "auto_diff", env: "DOXA_AUTO_DIFF", label: "auto-open the live diff", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Open the live diff beside a session the FIRST time it edits the worktree, once per session (doxa.diff.auto_open_enabled / PaneRuntimeMixin._maybe_auto_open_diff)", note: "OFF by default, and the default is the argument: opening the diff splits the group the session is in, halving the width of the transcript you are reading, and a surface that rearranges the screen mid-turn without being asked is worse than one you have to know about. ONCE per session, so closing it is final -- it never re-opens behind you. It never takes the keyboard (the prompt keeps focus), and on a window too narrow to split it REFUSES and says so rather than making an unusable sliver. The `diff N files +A −R` status chip is on either way and is how you see there are changes at all; F2 and /diff open the pane by hand at any time." },
    Setting { key: "spawn_sessions", env: "DOXA_SPAWN_SESSIONS", label: "let sessions spawn sessions", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Offer the model the spawn_session tool, which starts a SECOND daemon-backed DOXA session in this repo and hands it a task (doxa.session_ops)", note: "OFF by default, and this row is the ONLY place it can be turned on: it is read through config.raw, so ~/.doxa/config.toml or DOXA_SPAWN_SESSIONS in your own shell are the two doors, and nothing inside a repository you open is one of them -- a repo that could arm this would be arbitrary code execution on `doxa new` against an untrusted clone. Turning it on costs real money and real machine: each spawned session is another claude process (~294 MB measured), another linked worktree (~18 MB measured for this repo), and its own token spend, additive to the session that asked for it. Every call still stops and asks you, showing the exact task text the child will be given, in every permission mode except bypassPermissions; the depth/live-count/rate caps in doxa.session_ops are enforced regardless of mode and cannot be raised from here." },
    Setting { key: "agent_peer_send", env: "DOXA_AGENT_PEER_SEND", label: "let the model message other sessions", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Offer the model the peer_send tool, which delivers a message straight into another live DOXA session (doxa.operators)", note: "OFF by default, and this is the knob that retires DOXA's oldest statement about itself: until now the model had NO send tool, and every peer message crossed because a human typed /msg. Turning this on lets a model reach another session's context on its own initiative -- possibly in a different repository, since addressing is not scope-limited. Nothing about it is silent: every send is charged against a rate limit priced in DELIVERIES (a broadcast to 31 peers costs 31), every send is appended with its full body to $DOXA_HOME/peers/messages.jsonl, and both directions flash a light on the status bar. Read through config.raw, so this file or the environment are the two doors and nothing inside a repository you open is one of them. peer_list and peer_history stay available either way: seeing who is running changes nothing outside this process." },
    Setting { key: "peer_inbound_turns", env: "DOXA_PEER_INBOUND_TURNS", label: "let an arriving message start a turn", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "An incoming peer message starts a turn when this session is idle, and queues behind the running one when it is not (doxa.engine.SessionEngine._on_peer_frame)", note: "OFF by default, and deliberately NOT part of the row above: accepting messages and being woken by them are different grants, and a session may reasonably want the first without the second. With this off, an arriving message renders immediately and the model sees it on the next turn you start -- the behaviour DOXA has always had. With it on, a peer can spend this session's budget while you are not watching; a turn it started says so in its own first line and carries a peer- turn id into the ledger, so the spend has a traceable cause. A BROADCAST never starts a turn at any setting." },
    Setting { key: "session_budget_usd", env: "DOXA_SESSION_BUDGET_USD", label: "session spend ceiling ($)", category: "Session", kind: Kind::Number, choices: &[], default: "", read_only: false, help: "Stop STARTING turns once this session has spent this many dollars (doxa.budget.session_ceiling / doxa.engine.SessionEngine._budget_refusal)", note: "OFF by default -- unset, and nothing about any session changes -- and it is the row the two above make necessary. With peer_send and inbound turns armed, sessions address each other across repositories and wake each other, and until this row existed nothing anywhere bounded what that cost. It bounds STARTING a turn, not a turn in flight: the only dollar figure that exists arrives with the message that ENDS a turn, so a session may exceed this by the price of the one turn that crosses it, and DOXA will not multiply tokens by a price sheet to pretend otherwise (see doxa.vendors). A session at its ceiling is STOPPED, not dead: it says so in the transcript, every command still answers, and raising this number here lets the next prompt through with no restart. A turn an arriving PEER message started is refused exactly like a typed one -- that path is the reason this exists. Read through config.raw, so this file and the environment are the two doors and nothing inside a repository you open is one of them. Enforceable only on an engine that reports cost: codex and both API vendors report none, their spend reads as $0.00, and the row says so rather than appearing to work. doxa.fleet's --run-budget sets a run-wide total and derives this per session, because thirty-two individually reasonable limits multiply into one unreasonable one." },
    Setting { key: "permission_mode", env: "DOXA_PERMISSION_MODE", label: "permission mode", category: "Session", kind: Kind::Choice, choices: &["", "default", "acceptEdits", "plan"], default: "", read_only: false, help: "Claude permission mode for new sessions; use the permission chip or /mode for the current session", note: "Only default, acceptEdits and plan may be saved as Claude defaults. Codex uses a separate current-session picker: on-request, sandboxed auto, or full-access. Codex restores that session's choice; this setting does not select it for all new Codex sessions." },
    Setting { key: "linger_secs", env: "DOXA_LINGER_SECS", label: "linger secs", category: "Session", kind: Kind::Number, choices: &[], default: "120", read_only: false, help: "Seconds a daemon outlives its last client before finalizing (doxa.cli --linger default)", note: "" },
    Setting { key: "worktree_per_session", env: "DOXA_WORKTREE", label: "worktree per session", category: "Session", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Give each session its own git worktree (isolated edits, own branch doxa/<id>) instead of sharing the launch directory (doxa.worktrees.create)", note: "Off returns to today's behavior exactly: every session runs directly in the launch directory. A clean, unmerged worktree is removed with its branch when the session ends; anything committed or dirty is kept for you to merge by hand -- never auto-merged." },
    Setting { key: "restore_tabs", env: "DOXA_RESTORE_TABS", label: "restore tabs", category: "Session", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Reattach this repo's whole saved tab set -- order, pinned names, active tab, AND each tab's conversation -- on plain `doxa`, instead of the single most-recent session (doxa.tabsets)", note: "`doxa new` always starts exactly one fresh tab and never restores; `doxa attach <prefix>` stays the single-session path either way. Off returns to today's single-most-recent spawn-or-attach exactly -- the record is still WRITTEN (so turning this back on later has something to restore from), just never read on launch. Ended tabs resume only when resume_restored is on and their own provider history is verified; otherwise their saved transcripts remain read-only. Saved pane geometry and active-tab focus are restored within native bounds." },
    Setting { key: "resume_restored", env: "DOXA_RESUME_RESTORED", label: "resume restored tabs", category: "Session", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "A restored tab whose session ENDED comes back as a LIVE session continuing that conversation, instead of a read-only transcript (v0.56.0)", note: "Its own switch rather than a clause of `restore_tabs`, because it is the one part of restore that starts a PROCESS: one `claude` per resumed tab, spawned with --resume. It spends no tokens doing so -- the CLI loads that conversation from its own store and DOXA sends nothing until you type -- but a machine that comes back to six restored tabs starts six processes, and that is a choice worth being able to decline. Off is exactly v0.32.0's behaviour: read-only over the transcript, marked. A conversation the CLI has no history for (every session DOXA recorded before v0.56.0, when its id and the CLI's were still two different id spaces) falls back to read-only either way, and the tab says so." },
    Setting { key: "mesh_open_browser", env: "DOXA_MESH_OPEN_BROWSER", label: "open the mesh graph in a browser", category: "Session", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "`/mesh` opens the graph in this machine's browser as well as printing its URL. OFF by default", note: "Off because DOXA runs in terminals that have no browser to open: over SSH, in a container, on a headless box. `webbrowser.open` there either does nothing, prints a launcher's error over the TUI's own screen, or opens a text browser on top of it -- and the URL is printed either way, so nothing is lost by the default. On a desktop this saves a copy-paste." },
    Setting { key: "remote_enabled", env: "DOXA_REMOTE_ENABLED", label: "remote listening", category: "Remote", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Start the private Unix peer bridge with this daemon", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_allowed_logins", env: "DOXA_REMOTE_ALLOWED_LOGINS", label: "remote allowed logins", category: "Remote", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Comma-separated Tailscale logins (the Tailscale-User-Login header a `tailscale serve` loopback listener attaches) allowed to drive this session remotely (doxa.remote_policy.allowed_logins)", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_peers", env: "DOXA_REMOTE_PEERS", label: "remote peers", category: "Remote", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Other machines' peer bridges, comma-separated as label=host[:port] -- e.g. workstation=ws.tail1234.ts.net:47600 (doxa.peernet.endpoints)", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_bind", env: "DOXA_REMOTE_BIND", label: "remote bind address", category: "Remote", kind: Kind::Text, choices: &[], default: "127.0.0.1", read_only: false, help: "Legacy adapter preference; the native service binds a private Unix socket", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_port", env: "DOXA_REMOTE_PORT", label: "remote bind port", category: "Remote", kind: Kind::Number, choices: &[], default: "47600", read_only: false, help: "Port of configured remote peers; the native local listener uses a private Unix socket", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_proxy_uid", env: "DOXA_REMOTE_PROXY_UID", label: "remote proxy uid", category: "Remote", kind: Kind::Number, choices: &[], default: "0", read_only: false, help: "Unix UID of the local Tailscale Serve proxy allowed to pass Tailscale identity headers (doxa.peernet.proxy_uid)", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_allow_shell", env: "DOXA_REMOTE_ALLOW_SHELL", label: "remote allow shell", category: "Remote", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Let a remote driver run `!` shell commands (doxa.remote_policy.remote_allow_shell)", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "remote_allow_bypass", env: "DOXA_REMOTE_ALLOW_BYPASS", label: "remote allow bypass", category: "Remote", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Let a remote driver raise the permission mode to bypassPermissions (doxa.remote_policy.remote_allow_bypass)", note: "Applied by the native peer service; listener changes apply to new sessions. Remote delivery requires a verified reciprocal peer roster." },
    Setting { key: "lore", env: "DOXA_LORE", label: "memory", category: "Memory", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Does a session have memory at all -- the LORE snapshot in its system prompt, the per-turn refresh, the lore_* tools, and every write back into the store (doxa.engine.lore_enabled_default)", note: "ON by default, and the only switch in this file that REMOVES a capability rather than granting one. OFF means genuinely off: no snapshot is built, the lore_* operators are ABSENT from the model's tool list rather than present and refusing, and nothing is written -- no beliefs, no staged proposals, no session index. The transcript is still written (it is DOXA's own record; /resume and the transcript pane read it) and lore_core is still used to scrub secrets out of every line. This row is the DEFAULT: a session can be started with memory off individually (`doxa.daemon --no-lore`), which is what doxa.fleet uses to run memory-on and memory-off agents in one experiment." },
    Setting { key: "derive_secs", env: "DOXA_DERIVE_SECS", label: "derive secs", category: "Memory", kind: Kind::Number, choices: &[], default: "900", read_only: false, help: "Streaming-deriver debounce interval, seconds; 0 or off disables it (doxa.engine.derive_interval)", note: "" },
    Setting { key: "consult_floor", env: "DOXA_CONSULT_FLOOR", label: "consult floor", category: "Memory", kind: Kind::Number, choices: &[], default: "1.0", read_only: false, help: "bm25 relevance floor for the act-time belief consult; 0 disables it (doxa.engine.consult_floor)", note: "" },
    Setting { key: "graph_context", env: "DOXA_GRAPH_CONTEXT", label: "graph context", category: "Memory", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Add LORE's graph-backed context block (beliefs reached by a relation, ranked confidence-first) to the act-time consult (doxa.engine.graph_context_enabled)", note: "OFF by default. Calls LORE's OWN builder (lore_core.graph.context_candidates/render_context_block, LORE 0.44.0+) rather than a second ranking implementation, as a SEPARATE stage from the consult floor above -- the two toggle independently. Unlike the consult note (silent unless something clears the relevance floor), this block falls back to the best-supported beliefs in scope when nothing matches the prompt, so once on it rides EVERY turn, budgeted under its own char cap but real, recurring cost -- see the graph_context_chars row in /context." },
    Setting { key: "graph_view", env: "DOXA_GRAPH_VIEW", label: "belief graph view", category: "Memory", kind: Kind::Choice, choices: &["", "browser", "ascii"], default: "browser", read_only: false, help: "How the beliefs picker's 'g graph' row action shows a belief's neighbourhood (doxa.beliefgraph.graph_view_mode): 'browser' writes LORE's pan/zoom mermaid page under $DOXA_HOME/graphs and opens it; 'ascii' inserts LORE's own edge block as rows beneath the belief, in the TUI. Empty = browser.", note: "Per BELIEF, never whole-graph, and that is measured rather than chosen: a whole-graph view filtered to asserted relations fragmented 104 beliefs into 44 clusters, which mermaid stacks vertically -- 1188x13814 pixels, fitting on screen at 5%. A k-hop neighbourhood is connected by construction. 'ascii' is the answer for a headless or SSH session; 'browser' prints the file's path into the transcript either way, so one still ends up with something to scp when no browser opens." },
    Setting { key: "lore_root", env: "LORE_ROOT", label: "lore store", category: "Memory", kind: Kind::Text, choices: &[], default: "", read_only: true, help: "Where the belief store and session index live (lore_core.ROOT)", note: "Shared with the Claude Code LORE plugin -- one store, two carriers. Set LORE_ROOT to point elsewhere; a private store would fork your memory into two divergent halves. /setup makes and stickies this choice -- read_only here because this row is /setup's, not the settings modal's, to edit." },
    Setting { key: "nerd_font", env: "DOXA_NERD_FONT", label: "nerd font", category: "Appearance", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Use the nerd-font branch glyph instead of the branch sign in the status line (doxa.app.git_branch_symbol)", note: "" },
    Setting { key: "ctx_absolute", env: "DOXA_CTX_ABSOLUTE", label: "ctx: absolute tokens", category: "Appearance", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Print used/total tokens beside the ctx% chip (doxa.ui.labels.ctx_chip)", note: "Off, the numbers are still one hover away -- the ctx chip's tooltip carries them either way, and /usage prints them in full. On, they are dropped again on a terminal narrower than 100 columns rather than pushing other chips off the bar. A context limit the CLI never reported reads `?`; DOXA does not guess a window size." },
    Setting { key: "image_mode", env: "DOXA_IMAGE_MODE", label: "image mode", category: "Appearance", kind: Kind::Choice, choices: &["halfblock", "probe", "kgp", "sixel", "iterm2", "text"], default: "halfblock", read_only: false, help: "Render local Markdown image attachments: halfblock works in every terminal; probe detects Kitty, Sixel or iTerm2; text shows alt labels only.", note: "Only absolute image paths inside the verified session workspace render. Probe can pause briefly for terminal replies; forced graphics needs terminal support. File and pixel bounds apply." },
    Setting { key: "mermaid_renderer", env: "DOXA_MERMAID_RENDERER", label: "Mermaid renderer executable", category: "Appearance", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Optional absolute path to a reviewed local mmdc-compatible renderer. Empty keeps Mermaid source fences.", note: "Requires /usr/bin/bwrap and the renderer root below. Runs with no network and only system runtime, renderer package and a private temporary directory mounted. No renderer is downloaded or invoked from PATH." },
    Setting { key: "mermaid_renderer_root", env: "DOXA_MERMAID_RENDERER_ROOT", label: "Mermaid renderer package root", category: "Appearance", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "Absolute package directory containing the renderer executable and its dependencies.", note: "Use a dedicated reviewed package directory. HOME, DOXA state, session repositories and the default fleet root are refused as mounts; a narrow package directory elsewhere under HOME is allowed. The executable must resolve inside it." },
    Setting { key: "boot_banner", env: "DOXA_BOOT_BANNER", label: "boot banner", category: "Appearance", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Draw the DOXA mark above the session's opening identity block (doxa.banner.enabled)", note: "A plain on/off knob since v0.70.0 -- the drawn ring-and-triangle mark is the only form there is now, on every terminal; v0.58.0-0.65.0 also drew a raster logo.png on kitty-graphics/sixel terminals ('auto'/'image'), which read better than a half-block downscale but not better than the drawn mark, so it is gone rather than kept as a second look. A config.toml still holding 'auto', 'blocks' or 'image' from before this collapse keeps meaning on -- only an explicit off (or the pre-v0.49.0 0/false/no) turns the banner off. /img still shows the raster logo on request, in every tier this terminal answers for." },
    Setting { key: "key_notice", env: "DOXA_KEY_NOTICE", label: "unreachable key notice", category: "Appearance", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "One line at session start naming bound keys THIS terminal can't deliver and the slash command that reaches them instead (doxa.keyboard.notice_enabled / doxa.ui.labels.unreachable_notice)", note: "Empty exactly when there is nothing to say: a kitty-protocol terminal (nothing lost), or one whose protocol was never measured -- UNKNOWN is not LEGACY, so an unmeasured terminal stays silent rather than guessing (doxa/keyboard.py). Off returns to plain silence; /help and /doctor still report the same keys either way." },
    Setting { key: "context_grid", env: "DOXA_CONTEXT_GRID", label: "context grid style", category: "Appearance", kind: Kind::Choice, choices: &["", "glyphs", "ascii"], default: "", read_only: false, help: "Cell style for /context's 10x20 usage grid (doxa.ui.labels.context_grid_mode): 'glyphs' draws the draughts glyphs (⛀⛁⛶, Claude Code's own look); 'ascii' draws bracket cells ([#]/[ ]) for a terminal font that tofu's the Miscellaneous Symbols block. Empty = glyphs.", note: "DOXA cannot probe a terminal's own font coverage -- nothing in a terminal reports that -- so this is a manual switch, not detection: see tofu on the grid once, flip it here. Both styles read the identical measured cells and the identical per-category colors; only the two characters change." },
    Setting { key: "show_reasoning", env: "DOXA_SHOW_REASONING", label: "show reasoning", category: "Appearance", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Stream the model's summarized reasoning into a collapsed 'Reasoning' section per turn (doxa.engine._build_options / doxa.app.ReasoningSection)", note: "On: requests thinking={type: adaptive, display: summarized} at connect. Off: DOXA asks for nothing extra and leaves the model's own default alone -- it does NOT force thinking off, because some models (Claude Fable 5, Claude Mythos 5, Claude Mythos Preview) reject an explicit disable outright. On those models thinking runs (and is billed) regardless of this toggle; off only stops DOXA from asking to see it." },
    Setting { key: "background", env: "DOXA_BACKGROUND", label: "background", category: "Appearance", kind: Kind::Choice, choices: &["", "opaque", "transparent"], default: "opaque", read_only: false, help: "Paint the app's own background (opaque), or leave it unpainted so the terminal's own background shows through (transparent) (doxa.app.DoxaApp.get_theme_variable_defaults)", note: "DOXA can only stop PAINTING its background -- making the terminal WINDOW itself see-through is your terminal emulator's job (kitty's background_opacity, WezTerm's window_background_opacity, etc.). On an opaque terminal this setting changes nothing visible. Validated against dark terminal backgrounds, same as the rest of DOXA's palette -- a light terminal background will render body text at very low contrast." },
    Setting { key: "sidebar", env: "DOXA_SIDEBAR", label: "session sidebar", category: "Appearance", kind: Kind::BoolOn, choices: &[], default: "", read_only: false, help: "Show the collapsible session rail down the left of the window (/sidebar; F3 by default, configurable under Keys)", note: "THREE states, which is why this row is bool_on and not bool: empty means AUTO -- the rail appears once there is something for it to say (any collection, or a second session) and stays hidden before that, the hide-at-zero discipline the context chip and the group tab strips already follow. 1 pins it open, 0 pins it shut, and the sidebar shortcut writes one of those two, so the first toggle ends the guessing for good. The rail REFUSES to open on a window too narrow to hold it and the panes both (doxa.layout.sidebar_refusal): it says so rather than squeezing a pane below its floor." },
    Setting { key: "rail_entries", env: "DOXA_RAIL_ENTRIES", label: "rail entries", category: "Appearance", kind: Kind::Choice, choices: &["sessions", "panes"], default: "sessions", read_only: false, help: "Show one row per session or one row per open pane; /collection view switches it.", note: "Pane view navigates the existing pane instead of moving its active tab. Hidden tabs contribute urgency to the pane row. Unknown or mixed project roots do not receive a project hue." },
    Setting { key: "collection_sort", env: "DOXA_COLLECTION_SORT", label: "rail group order", category: "Appearance", kind: Kind::Choice, choices: &["manual", "urgency"], default: "manual", read_only: false, help: "Order collection and project headings by their most urgent session; /collection sort toggles this setting.", note: "Manual is the default. Urgency only moves whole groups after marks settle and the pointer and keyboard have left the rail. Session order within each group stays fixed; Past sessions stay last." },
    Setting { key: "sidebar_width", env: "DOXA_SIDEBAR_WIDTH", label: "session sidebar: width", category: "Appearance", kind: Kind::Number, choices: &[], default: "25", read_only: false, help: "Columns the session rail occupies (doxa.layout.SIDEBAR_WIDTH; clamped to 22–41). Drag the rail's right edge, or Alt+Shift+←/→, to change it", note: "Clamped, never rejected: 25 is derived as the rail's own chrome (9 columns) plus half the tab-label cap the strip writes at, 22 is the width below which a row cannot show the label floor the tab strip keeps legible, and 41 is the width at which the whole capped label fits and wider buys nothing. All three moved by two in v1.5.0: doxa.layout.SIDEBAR_CHROME was re-measured against the rail's DEEPEST row -- a tab row under a pane entry under a heading -- which v1.2.0 added without re-pricing. A drag and the keys write this row, and both refuse at the same floor opening the rail refuses at." },
    Setting { key: "clock_show", env: "DOXA_CLOCK_SHOW", label: "clock: show", category: "Appearance", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Show the fixed-width clock at the right edge of the tab bar (doxa.clock.ClockConfig)", note: "The one bool setting in this app that defaults ON -- an empty field here still means the clock shows; type 0 to turn it off." },
    Setting { key: "clock_date", env: "DOXA_CLOCK_DATE", label: "clock: show date", category: "Appearance", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Prefix the clock with %Y-%m-%d (doxa.clock.builtin_format)", note: "" },
    Setting { key: "clock_hour", env: "DOXA_CLOCK_HOUR", label: "clock: hour format", category: "Appearance", kind: Kind::Choice, choices: &["", "12", "24"], default: "24", read_only: false, help: "12- or 24-hour clock (doxa.clock.builtin_format)", note: "" },
    Setting { key: "clock_seconds", env: "DOXA_CLOCK_SECONDS", label: "clock: show seconds", category: "Appearance", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Show :SS; also switches the clock's one timer from minute- to second-aligned (doxa.clock.seconds_until_boundary)", note: "" },
    Setting { key: "clock_tz", env: "DOXA_CLOCK_TZ", label: "clock: timezone", category: "Appearance", kind: Kind::Text, choices: &[], default: "", read_only: false, help: "IANA zone name, e.g. Europe/Berlin; empty = system local (doxa.clock.resolve_tz)", note: "An unresolvable name falls back to system local time, visibly (the clock's tooltip says so) rather than silently." },
    Setting { key: "clock_format", env: "DOXA_CLOCK_FORMAT", label: "clock: custom format", category: "Appearance", kind: Kind::Format, choices: &[], default: "", read_only: false, help: "strftime format overriding the toggles above (doxa.clock.render)", note: "Validated on save (a value strftime rejects is not stored); a value that becomes invalid later (a hand-edited file, an env var) falls back to the built-in format at render time, visibly, rather than crashing the clock." },
    Setting { key: "notify", env: "DOXA_NOTIFY", label: "notify", category: "Notifications", kind: Kind::Choice, choices: &["auto", "always", "off"], default: "auto", read_only: false, help: "When to send desktop notifications: auto (only while the terminal window is unfocused), always, or off (doxa.notify)", note: "" },
    Setting { key: "notify_update", env: "DOXA_NOTIFY_UPDATE", label: "notify: update available", category: "Notifications", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Notify when /update has something to pull (doxa.notify.notify_update_available)", note: "" },
    Setting { key: "notify_lore", env: "DOXA_NOTIFY_LORE", label: "notify: lore review", category: "Notifications", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Notify when LORE stages memory proposals; off also silences lore_core's own in-process notification (LORE_NOTIFY) -- see doxa.notify.sync_lore_notify_env", note: "This is lore_core's OWN banner, which knows nothing about window focus. 'notify: proposals staged' below is DOXA's focus-gated replacement for it, and while that one is on this one is held silent so a single staged batch produces a single notification." },
    Setting { key: "notify_staged", env: "DOXA_NOTIFY_STAGED", label: "notify: proposals staged", category: "Notifications", kind: Kind::BoolOn, choices: &[], default: "1", read_only: false, help: "Notify when the streaming background reviewer stages memory proposals (doxa.notify.notify_staged)", note: "Fires off the streaming deriver (derive_secs), names the tab and quotes the first proposal, and is gated like every other trigger above -- so it stays quiet while you are looking at DOXA. Turn it off and 'notify: lore review' decides on its own again (doxa.notify.sync_lore_notify_env)." },
    Setting { key: "notify_needs_input", env: "DOXA_NOTIFY_NEEDS_INPUT", label: "notify: needs input", category: "Notifications", kind: Kind::Bool, choices: &[], default: "", read_only: false, help: "Notify when a session is waiting on you", note: "OFF by default -- the ONLY notification-worthy turn outcome (a plain finished response is not one; see the module docstring of doxa.notify) is still opt-in, on the owner's own call. Fires on an AskUserQuestion or a permission request the CLI would have prompted on (doxa.engine's can_use_tool callback) -- while it's attached, gated like every other trigger above; a fully detached session (nobody attached at all) always notifies once this is on, since there is no window to blink instead." },
    Setting { key: "", env: "DOXA_HOME", label: "doxa home", category: "Paths", kind: Kind::Text, choices: &[], default: "", read_only: true, help: "Durable DOXA state: this config, the window layout", note: "" },
    Setting { key: "", env: "DOXA_RUNTIME_DIR", label: "runtime dir", category: "Paths", kind: Kind::Text, choices: &[], default: "", read_only: true, help: "Ephemeral endpoints: daemon sockets and the peer registry (doxa.peers.runtime_dir)", note: "Deliberately NOT under ~/.doxa: home directories can be NFS (AF_UNIX misbehaves) and stale sockets must not outlive a reboot." },
];

#[derive(Debug, Clone)]
pub struct Row { pub setting: &'static Setting, pub value: String, pub source: String, pub shadowed: bool, pub stored: String }
pub fn find(key: &str) -> io::Result<&'static Setting> {
    SETTINGS.iter().find(|s| !key.is_empty() && s.key == key).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, format!("unknown setting {key}")))
}
pub fn config_path() -> io::Result<std::path::PathBuf> { Ok(crate::operations::doxa_home()?.join("config.toml")) }
pub fn raw_from(config: &toml::Table, setting: &Setting, override_value: Option<&str>, engine: &str) -> String {
    if let Some(value) = override_value.filter(|v| !v.trim().is_empty()) { return value.trim().into(); }
    if setting.kind == Kind::Key {
        let mut effective = config.clone();
        crate::keybindings::migrate_legacy_defaults(&mut effective);
        return doxa_state::raw_setting(None, &effective, setting.key).trim().into();
    }
    if setting.key == "model" && engine != "claude" {
        return config.get("models").and_then(toml::Value::as_table).and_then(|v| v.get(engine)).and_then(toml::Value::as_str).unwrap_or("").trim().into();
    }
    doxa_state::raw_setting(None, config, setting.key).trim().into()
}
pub fn raw(key: &str) -> String {
    let Ok(s) = find(key) else { return String::new(); };
    let config = config_path().map(|p| doxa_state::load_config(&p)).unwrap_or_default();
    raw_from(&config, s, std::env::var(s.env).ok().as_deref(), "claude")
}
pub fn enabled(key: &str) -> bool {
    let Ok(s) = find(key) else { return false; };
    let value = raw(key);
    let value = if value.is_empty() { s.default } else { &value };
    !value.is_empty() && !matches!(value.to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off")
}
pub fn rows(engine: &str, session_model: Option<&str>) -> io::Result<Vec<Row>> {
    let path = config_path()?;
    let config = doxa_state::load_config_checked(&path)?;
    let home = crate::operations::doxa_home()?;
    let mut rows = Vec::new();
    for s in SETTINGS {
        let override_value = std::env::var(s.env).ok().filter(|v| !v.trim().is_empty());
        let shadowed = override_value.is_some();
        let stored = raw_from(&config, s, None, engine);
        let mut value = raw_from(&config, s, override_value.as_deref(), engine);
        let mut source = if shadowed { format!("env {} — overrides config", s.env) } else if (s.key != "model" && config.contains_key(s.key)) || !stored.is_empty() { "config.toml".into() } else { "default".into() };
        if value.is_empty() { value = s.default.into(); }
        if s.env == "DOXA_HOME" { value = home.display().to_string(); }
        if s.env == "DOXA_RUNTIME_DIR" { value = crate::discovery::runtime_dir()?.display().to_string(); }
        if s.env == "LORE_ROOT" { value = override_value.or_else(||config.get("lore_root").and_then(toml::Value::as_str).map(str::to_owned)
            .filter(|v| !v.trim().is_empty())).unwrap_or_else(|| std::env::var("HOME").map(|v| format!("{v}/.claude/lore")).unwrap_or_default()); }
        if matches!(s.kind, Kind::Bool | Kind::BoolOn) && !(s.key == "sidebar" && value.is_empty()) {
            value = if value.is_empty() || matches!(value.to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off") { "off" } else { "on" }.into();
        }
        if s.key == "sidebar" && value.is_empty() { value = "auto".into(); }
        if s.key == "model" { if let Some(model) = session_model.filter(|v| !v.is_empty()) {
            if model != value { source = format!("session — {engine} default is {}", if value.is_empty() { "unset" } else { &value }); }
            else { source = "session".into(); } value = model.into();
        }}
        rows.push(Row { setting: s, value, source, shadowed, stored });
    }
    Ok(rows)
}
pub fn coerce(s: &Setting, value: Option<&str>) -> io::Result<Option<toml::Value>> {
    if s.read_only || s.key.is_empty() { return Err(io::Error::new(io::ErrorKind::PermissionDenied, format!("{} is read-only. {}", s.label, s.note))); }
    let Some(value) = value.map(str::trim).filter(|v| !v.is_empty()) else { return Ok(None); };
    let invalid = |reason: &str| io::Error::new(io::ErrorKind::InvalidInput, format!("{}: {reason}", s.key));
    if value.len() > 4096 || value.chars().any(char::is_control) { return Err(invalid("value must be at most 4096 bytes and contain no control characters")); }
    Ok(Some(match s.kind {
        Kind::Bool | Kind::BoolOn => {
            let on = match value.to_ascii_lowercase().as_str() { "on"|"true"|"1"|"yes" => true, "off"|"false"|"0"|"no" => false, _ => return Err(invalid("accepts on or off")) };
            if s.kind == Kind::BoolOn { toml::Value::String(if on { "1" } else { "0" }.into()) } else { toml::Value::Boolean(on) }
        },
        Kind::Number => {
            let n = if s.key == "derive_secs" && value == "off" { 0.0 } else { value.parse::<f64>().map_err(|_| invalid("must be a nonnegative finite number"))? };
            if s.key=="fleet_review_threshold"&&n>1.0{return Err(invalid("must be between 0 and 1"));}
            if !n.is_finite() || n < 0.0 { return Err(invalid("must be a nonnegative finite number")); }
            if s.key == "linger_secs" && n > crate::launch::MAX_LINGER_SECS { return Err(invalid("must be between 0 and 31536000 seconds")); }
            if matches!(s.key, "remote_port"|"remote_proxy_uid") && n.fract() != 0.0 { return Err(invalid("must be an integer")); }
            if matches!(s.key,"docker_memory_bytes"|"docker_pids"|"docker_disk_soft_limit_bytes"|"docker_disk_free_floor_bytes"){
                if n.fract()!=0.0||n>i64::MAX as f64{return Err(invalid("must be a bounded integer"));}
                if s.key=="docker_memory_bytes"&&!(134217728.0..=1099511627776.0).contains(&n){return Err(invalid("must be between 128 MiB and 1 TiB"));}
                if s.key=="docker_pids"&&!(16.0..=65536.0).contains(&n){return Err(invalid("must be between 16 and 65536"));}
                if s.key=="docker_disk_soft_limit_bytes"&&!(134217728.0..=1099511627776.0).contains(&n){return Err(invalid("must be between 128 MiB and 1 TiB"));}
                if s.key=="docker_disk_free_floor_bytes"&&!(536870912.0..=1099511627776.0).contains(&n){return Err(invalid("must be between 512 MiB and 1 TiB"));}
                return Ok(Some(toml::Value::Integer(n as i64)));
            }
            if s.key=="docker_cpus"&&!(0.25..=256.0).contains(&n){return Err(invalid("must be between 0.25 and 256"));}
            toml::Value::Float(if s.key == "sidebar_width" { n.clamp(22.0,41.0) } else { n })
        },
        Kind::Choice => { if !s.choices.contains(&value) { return Err(invalid(&format!("accepts {}", s.choices.join(" | ")))); } toml::Value::String(value.into()) },
        Kind::Format => { crate::preferences::validate_clock_format(value).map_err(|_| invalid("invalid strftime format"))?; toml::Value::String(value.into()) },
        Kind::Key => { let chord = crate::keybindings::Chord::parse(value).map_err(|e| invalid(&e.to_string()))?; toml::Value::String(chord.map(|c| c.display()).unwrap_or_else(|| "none".into())) },
        Kind::Text => {if matches!(s.key,"fleet_alignment_supervisor"|"fleet_message_judge"){let model=doxa_fleet::judge::Model::parse(value).map_err(|_|invalid("requires a supported provider:model"))?;if s.key=="fleet_alignment_supervisor"&&model.provider=="jev"{return Err(invalid("Jev judges messages; select an LLM supervisor"));}}toml::Value::String(value.into())},
    }))
}
pub fn save(path: &Path, edits: &[(String, Option<String>)], engine: &str) -> io::Result<()> {
    let mut parsed = Vec::new();
    for (key, value) in edits {
        let s = find(key)?;
        if std::env::var(s.env).ok().is_some_and(|v| !v.trim().is_empty()) { return Err(io::Error::new(io::ErrorKind::PermissionDenied, format!("{} overrides config.toml; unset it before changing {key}", s.env))); }
        parsed.push((s, coerce(s, value.as_deref())?));
    }
    let key_edited = parsed.iter().any(|(setting, _)| setting.kind == Kind::Key);
    doxa_state::update_config(path, |config| {
        crate::keybindings::migrate_legacy_defaults(config);
        for (s, value) in parsed {
            if s.key == "model" && engine != "claude" {
                if !config.contains_key("models") { config.insert("models".into(), toml::Value::Table(toml::Table::new())); }
                let models = config.get_mut("models").and_then(toml::Value::as_table_mut).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData,"models must be a table"))?;
                if let Some(value) = value { models.insert(engine.into(),value); } else { models.remove(engine); }
            } else if let Some(value) = value { config.insert(s.key.into(),value); } else { config.remove(s.key); }
        }
        if key_edited { config.insert("keybindings_schema".into(), toml::Value::Integer(2)); }
        crate::keybindings::Bindings::from_config(config)?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn catalog_covers_python119_categories_and_conservative_defaults() {
        for key in ["engine","effort","allow_bypass","spawn_sessions","agent_peer_send","peer_inbound_turns","session_budget_usd","permission_mode","restore_tabs","resume_restored","lore","derive_secs","consult_floor","graph_context","graph_view","ctx_absolute","context_grid","boot_banner","show_reasoning","background","sidebar","rail_entries","collection_sort","sidebar_width","clock_show","clock_date","clock_hour","clock_seconds","clock_tz","clock_format","notify","notify_update","notify_lore","notify_staged","notify_needs_input"] {assert!(find(key).is_ok(),"missing {key}");}
        assert_eq!(find("lore").unwrap().default,"1");assert_eq!(find("sidebar").unwrap().default,"");assert_eq!(find("collection_sort").unwrap().default,"manual");assert_eq!(find("rail_entries").unwrap().default,"sessions");assert_eq!(find("notify").unwrap().default,"auto");assert_eq!(find("notify_needs_input").unwrap().default,"");
        assert_eq!(find("engine").unwrap().choices,&["","claude","codex","deepseek","glm"]);
        assert!(!find("image_mode").unwrap().read_only);assert!(find("lore_root").unwrap().read_only);
    }
    #[test]
    fn save_preserves_tables_dates_default_off_and_engine_models() {
        let dir=tempfile::tempdir().unwrap();std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();let path=dir.path().join("config.toml");
        std::fs::write(&path,"model='claude-kept'\nfuture=1979-05-27\n[projects]\n'/repo'='teal'\n[models]\ndeepseek='existing'\n").unwrap();
        save(&path,&[("model".into(),Some("codex-own".into())),("lore".into(),Some("off".into())),("sidebar_width".into(),Some("99".into())),("notify_needs_input".into(),Some("on".into()))],"codex").unwrap();
        let config=doxa_state::load_config_checked(&path).unwrap();assert_eq!(config["model"].as_str(),Some("claude-kept"));assert_eq!(config["models"]["codex"].as_str(),Some("codex-own"));assert_eq!(config["models"]["deepseek"].as_str(),Some("existing"));assert_eq!(config["lore"].as_str(),Some("0"));assert_eq!(config["sidebar_width"].as_float(),Some(41.0));assert_eq!(config["notify_needs_input"].as_bool(),Some(true));assert!(config["future"].is_datetime());assert_eq!(config["projects"]["/repo"].as_str(),Some("teal"));assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode()&0o777,0o600);
        assert_eq!(raw_from(&config,find("model").unwrap(),None,"codex"),"codex-own");assert_eq!(raw_from(&config,find("model").unwrap(),Some("env-model"),"codex"),"env-model");
        save(&path,&[("model".into(),None)],"codex").unwrap();assert!(!doxa_state::load_config_checked(&path).unwrap()["models"].as_table().unwrap().contains_key("codex"));
    }
    #[test]
    fn malformed_or_invalid_saves_do_not_erase_state() {
        let dir=tempfile::tempdir().unwrap();std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();let path=dir.path().join("config.toml");std::fs::write(&path,"[broken").unwrap();
        assert!(save(&path,&[("clock_show".into(),Some("off".into()))],"claude").is_err());assert_eq!(std::fs::read_to_string(&path).unwrap(),"[broken");
        for (key,value) in [("linger_secs","NaN"),("consult_floor","-1"),("effort","invalid"),("permission_mode","bypassPermissions"),("notify","sometimes"),("collection_sort","live"),("clock_format","%H\n%s"),("lore","maybe")] {assert!(coerce(find(key).unwrap(),Some(value)).is_err(),"{key}");}
        for key in ["docker_disk_soft_limit_bytes","docker_disk_free_floor_bytes"] {
            assert!(coerce(find(key).unwrap(),Some("0")).is_err());
            assert!(coerce(find(key).unwrap(),Some("1.5")).is_err());
            assert!(coerce(find(key).unwrap(),Some("1099511627777")).is_err());
        }
        assert_eq!(coerce(find("docker_disk_soft_limit_bytes").unwrap(),Some("21474836480")).unwrap().unwrap().as_integer(),Some(21474836480));
    }
    #[test]
    fn keybinding_collision_rejects_write_and_valid_change_persists() {
        let dir=tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();
        let path=dir.path().join("config.toml");
        save(&path,&[("key_new_tab".into(),Some("Alt+N".into()))],"claude").unwrap();
        let before=std::fs::read_to_string(&path).unwrap();
        assert_eq!(doxa_state::load_config_checked(&path).unwrap()["key_new_tab"].as_str(),Some("Alt+N"));
        assert!(save(&path,&[("key_tools".into(),Some("Alt+N".into()))],"claude").is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(),before);
        assert!(save(&path,&[("key_new_tab".into(),Some("Ctrl+C".into()))],"claude").is_err());
        assert_eq!(std::fs::read_to_string(&path).unwrap(),before);
        save(&path,&[("key_tools".into(),Some("none".into()))],"claude").unwrap();
        assert_eq!(doxa_state::load_config_checked(&path).unwrap()["key_tools"].as_str(),Some("none"));
    }
    #[test]
    fn key_catalog_matches_runtime_registry() {
        let catalog=SETTINGS.iter().filter(|setting| setting.kind==Kind::Key).collect::<Vec<_>>();
        assert_eq!(catalog.len(),crate::keybindings::DEFINITIONS.len());
        for definition in crate::keybindings::DEFINITIONS {
            let setting=find(definition.key).unwrap();
            assert_eq!(setting.kind,Kind::Key);
            assert_eq!(setting.default,definition.default);
            assert_eq!(setting.label,definition.label);
        }
    }
    #[test]
    fn saved_old_lifecycle_shortcuts_migrate_before_other_settings_are_written() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.path().join("config.toml");
        std::fs::write(&path, "key_close_tab='Ctrl+X'\nkey_close_tab_alt='Ctrl+W'\nkey_stop='Alt+X'\n").unwrap();
        let old = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(raw_from(&old, find("key_close_tab").unwrap(), None, "claude"), "Ctrl+W");
        save(&path, &[("clock_show".into(), Some("off".into()))], "claude").unwrap();
        let migrated = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(migrated["keybindings_schema"].as_integer(), Some(2));
        assert_eq!(migrated["key_close_tab"].as_str(), Some("Ctrl+W"));
        assert_eq!(migrated["key_close_tab_alt"].as_str(), Some("Delete"));
        assert_eq!(migrated["key_stop"].as_str(), Some("Ctrl+X"));
        save(&path, &[("key_stop".into(), Some("Alt+X".into()))], "claude").unwrap();
        let customized = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(crate::keybindings::Bindings::from_config(&customized).unwrap()
            .display(crate::keybindings::Action::Stop), "Alt+X");
    }
}
