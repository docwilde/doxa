# Native Codex coordinator allowance

The CLI flag `--supervisor-account-turn-limit N` opts a native Codex engine
coordinator into a finite allowance of 1–32 provider-account dispatch attempts.
Choose an exact model with `--supervisor codex:MODEL`. This flag is currently
CLI-only; the fleet launch form does not collect it.

Provider-account charges remain **unknown and outside `--run-budget`**. This is
an attempt limit, not a dollar cap or a statement that the account is free. The
ordinary role isolation defaults and owner confirmations still apply.

With this flag, all workers must use reviewed API-only Router candidates.
`--run-budget` first reserves independent review spending, then divides the
remaining API allowance equally among workers. The coordinator receives no API
dollar allocation. For example, a $0.42 run with $0.21 reserved for reviews and
three workers gives each worker $0.07, separately from the coordinator's account
attempt allowance. Run `doxa fleet start ... --dry-run` to inspect the frozen
roles, account policy and API allocation before launch.

Each DOXA provider dispatch reserves one attempt durably before invoking the
native Codex host. Owner prompts, peer-triggered and queued turns, attachment
prompts and explicit compaction prompts use the same path. Cancellation,
refusal and errors consume attempts. This counts DOXA dispatch attempts;
backend-internal RPCs or hidden automatic compaction are not individually
counted. No unknown attempt is retried or refunded automatically.

The allowance stays in the same private DOXA state root across daemon resume,
respawn and isolation migration. The session, engine, role, exact model and
limit remain bound to its record and initial policy. Missing, changed, corrupt
or unsettled records refuse new dispatches. Keep both
`account-turns/SESSION.json` and `account-turns/SESSION.policy.json`; deleting
one is not a way to replenish an allowance. Account-limited coordinators cannot
publish DOXA child-spawn tools or spawn child sessions with inherited limits.

Journal admission rejects shared-writable ancestors and owner-controlled
symlink aliases. Root-owned operating-system aliases and root-owned sticky
temporary ancestors are accepted. The final directory must be private and
owned by the current user. Reads and atomic writes use a pinned directory
descriptor and recheck its device/inode. This protects ordinary path replacement;
it does not promise protection against an owner deliberately rewriting all of
their own state.

For API workers, an optional reviewed Router candidate field
`"max_tool_requests": 4` limits actual HTTP generation requests per turn,
including the final answer request. Values 1–25 are accepted. Omitting it keeps
the existing default of 25 and preserves old serialized config hashes. The
candidate's conservative worker hold uses the same count, context/input bound,
output bound and declared prices. A tool call limit alone does not imply that
the final answer fits: reserve enough requests for that answer too. Unknown
usage retains the full reservation and stops further spending.
