# Fleet readiness work log

Scope: user item 1, installed beta.63 fleet acceptance. Worktree `doxa-readiness-fleet`, base `c2f80bed`. Owner sessions and global configuration are outside scope. All Docker operations use the existing rootless Engine and pinned beta.63 worker image.

2026-10-20: Read fleet access/supervision contracts, native integration coverage, launch/menu, transport and budget implementations. Installed release and image match. DeepSeek/GLM private saved keys exist; no API environment credentials or Jev/SubQ decision store. Claude/Codex subscriptions exist but billing remains unknown. No model calls made.

Budget finding: ordinary vendor `BudgetHost` admits before checking the current turn's maximum cost and settles afterward. Direct vendor tool loops have 24 steps, 4096 output-token request defaults and 8 MiB history. A $1 run ceiling therefore is not a hard total-call allocation. Router transport has explicit input/output bounds but no saved decision credential is available. Notified parent before inference. Preparing installed-runtime acceptance with independent reviewer deliberately unavailable so initial dispatch stays held; this must not be represented as live provider or supervisor-quality acceptance.
