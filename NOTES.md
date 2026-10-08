Per-session Docker isolation implementation

The host daemon retains LORE, transcripts, approvals, peer routing and API vendor credentials. Provider CLIs run over their existing stdio protocols inside a private rootless Docker container. Docker profiles explicitly distinguish open egress from no network; neither claims hardened egress or hard disk quota. Independent clones contain their own Git store and no host-path remote. Native remains the default.

Implementation work and verification are recorded here as they complete.

Implemented the doxa-isolation host runtime and worker binary, local rootless/cgroup/image preflight, independent clone, four private mounts, container inspect/reconcile, effective network verification, idle confirmed open/offline transitions and provider EOF ownership. Claude stdio hooks stay host-owned; protected Codex uses a narrowly scoped host compaction broker and an ownership channel inside its worker. API vendor HTTP keys remain host-owned and workspace reads remain bounded to the cloned checkout.

CLI --isolation and owner configuration select new-session profile. TUI new-session form exposes the same three levels; actual runtime assertions drive the chip, its details and /isolation PROFILE --confirm. Runtime admission serializes changes with turn admission and refuses busy/queued/pending approval changes. Child launches inherit the current recorded Docker profile. Native/Docker migration is being implemented separately by the root team's migration worker against isolation_migration_plan.

Cargo check for doxa-isolation, doxa-daemon and doxa-tui passed before the initial checkpoint. Targeted tests and task-local rootless Docker smoke follow; no live install or publication performed by this worker.
