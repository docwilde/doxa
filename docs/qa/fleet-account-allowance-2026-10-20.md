# Account allowance and request bounds on 2026-10-20

The beta.64 acceptance continuation adds an explicit CLI-only native Codex
coordinator account-attempt allowance and an optional reviewed API request cap.
[Operator instructions](../fleet-account-allowance.md) describe their scopes.
No new provider-backed fleet has been launched for this continuation. The prior
$0.477688 conservative pilot holds remain retained; these local fixtures add no
provider charges and do not establish successful live handoff acceptance.

The account journal reserves before each DOXA dispatch, including queued peers,
attachment prompts and explicit compaction prompts. Refusal/error/cancellation
does not refund an attempt. Missing or duplicate terminal events leave the
attempt unsettled. Resume requires the original session/model/role/limit,
initial policy and settled journal. Direct daemon startup enforces this even if
the caller removes the allowance environment. A historical resume without a
model restores its verified exact saved model before Codex startup; malformed
terminal categories are also refused there. Child spawning is refused rather
than inheriting an unrelated fresh allowance. Isolation migration compares the
same observed allowance before stop and after respawn. Charges remain unknown
and outside the worker API ledger; backend-internal RPCs are not individually
counted by this dispatch policy.

Journal paths use a trusted canonical ancestor chain and a pinned final private
directory descriptor, with device/inode rechecks and descriptor-relative reads,
locks and atomic writes. Root-owned OS aliases and root-owned sticky temporary
ancestors are admitted; owner symlink aliases and shared-writable ancestors
refuse. A replaced directory, replacement symlink or changed ancestor
permissions refused before fixture-provider dispatch. This does not protect
against an owner deliberately rewriting their complete private state.

The Router candidate's optional `max_tool_requests` includes the final answer
HTTP request. Omission preserves the default 25 and prior serialized hashes.
At a reviewed 40,000 context/input-byte bound, 4,096 aggregate output tokens,
four HTTP requests, and declared $0.30/$1.20 per million input/output units,
the production reservation function returns exactly $0.048000. These declared
rates and bounds are conservative accounting inputs, not measured pilot usage.
Unknown usage continues to retain the full hold.

Local validation passed: five account-host lifecycle/path tests, the child-spawn
guard regression, two direct-daemon startup tests covering eleven policy/resume
cases and two custom-home path cases, three frontend account role/history/
migration tests, all 36 fleet-control tests, and the production reservation
regression. The broader account filters each passed seven tests, including
pre-existing account UI/startup checks. Earlier six-request/final-answer and
unknown-reservation tests are recorded with their source commits; no new live
API success is inferred from these fixtures. Protected integration CI, exact
model availability, full-growth independent-review preflight and a frozen live
pilot remain separate gates.
