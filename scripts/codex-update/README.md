# Protected Codex release updates

`codex-update.yml` follows published, non-prerelease `rust-vX.Y.Z` releases
from `openai/codex`. Every candidate resolves its official tag to an exact
peeled commit. It carries the existing reviewed protection patch forward;
it cannot invent a semantic port. Upstream changes to compaction, session-turn,
hook, approval, permission, execution surfaces or new Rust modules require
semantic review even when the old patch applies. The upstream diff and changed
path inventory are hashed; compatible proposals include that identity. Patch
conflicts, a changed release-stamping shape or a missing guard test also stop
the update and open/update a visible
**Protected Codex update blocked** issue. The installed provider stays pinned.

The workflow is disabled until the repository variable
`CODEX_PROTECTED_UPDATES_ENABLED` is `true`. Activate it after the first
0.163.0 protected port, compiled proofs and repository CI are validated.
This source change does not enable it, install a provider, change repository
settings, add a token, or assert that a hosted candidate has built successfully.

The six-hour discovery selects the oldest newer stable version from its bounded
release inventory, so consecutive releases are processed in order. Drafts,
release candidates, nightly tags and SDK/helper releases are ineligible.
Explicit source commit, patch digest, protection contract and provider ID are
written together to `scripts/codex-protected/pin.json` in each release PR.
Current reviewed values come from installer/engine literals; there is no
floating upstream binary or silently trusted cached checkout. Previous artifact
receipt pins rotate exactly one generation for activation and rollback.

## Gates and publication

The candidate job has read-only repository permissions and no provider account
state. Its builder uses a fresh private home, one Cargo job, the installer's
pinned Rust/V8 inputs and one verified **8 GiB / no-swap** user-systemd scope
covering the entire verifier and its children. Installer build commands inherit
that scope instead of creating sibling scopes. It builds the real
protected server and helper, verifies their build fingerprints, runs every
reviewed compaction test family with a nonzero test inventory, runs compiled
automatic-compaction refusal/default-window proofs, and exercises the actual
helper plus its missing-helper negative control against loopback fixtures.
Native engine permission/launcher/provenance tests also run. These tests use
synthetic local Responses fixtures; they are not real account availability,
billing or model-quality evidence and never submit a paid model request.

The exact recipe, source files, source commit, patch and pin hashes are checked
before and after compiled verification. A final receipt binds those values to
the original verifier run. Its digest and the candidate digest cross the job
boundary as Actions outputs, independently of the source artifact. Publisher
and recovery validate this binding; a successful job for another candidate is
insufficient.

Only bounded source files, this receipt and a hashed candidate recipe cross to a fresh
publisher. Trusted base-branch Python independently clones the official tag,
reapplies the same reviewed patch and recreates every proposed byte before it
uses a write token. No artifact program or upstream build script runs there.
The diff is limited to explicitly listed active pin files/literals with exact
version boundaries, the manifest,
all internal DOXA package versions/the matching lock entries and a short
CHANGELOG entry. Historical docs and legacy installation paths retain their
old identity, including prefix-colliding version examples. Ordinary whitespace
checks exclude only `scripts/codex-protected/precompact.patch`: its literal
space-only context lines are required by unified-diff syntax and validated by
exact patch application and canonical regeneration. Other whitespace errors
still block publication. Publication refuses a changed base or an existing proposal.

The publisher uses only the ephemeral `GITHUB_TOKEN`, with scoped Actions,
contents, PR and issue permissions. It never copies an owner PAT. It creates a
release PR, explicitly dispatches extra Rust and Android CI, then waits for
the **actual `pull_request` workflows**: `rust`, `macos` and `android` must all
pass. Public branch metadata must expose protected `rust` from GitHub Actions,
enforced for everyone; the updater also requires the exact current main base.
The normal merge API enforces every configured check and review. Full protection
REST requires Administration permission, which the job token does not receive.
There is no self-review or admin bypass.

GitHub currently puts bot-created PR runs behind an approval gate. The helper
attempts the documented approval endpoint only for the exact automation
branch/head and workflow; platform refusal becomes a blocked update. A
`workflow_dispatch` success is never substituted for a protected PR check.
This platform capability and the hosted runner's build capacity still need
one real activation run; the local unit tests do not establish them.

After all gates, a normal protected squash merge is followed immediately in
the same job by its annotated `v<DOXA version>` tag and GitHub release, using
the exact CHANGELOG text. Tags bind the returned merged commit, never a later
main HEAD. Existing conflicting tags/releases refuse overwrite. Repository
auto-merge does not need to be enabled.

## Interrupted transaction recovery

GitHub merge, tag and release APIs do not provide a cross-service atomic
transaction. The workflow performs them together and retains the source
candidate for 90 days so an interrupted tag/release step can resume without
merging another bump. A blocked issue links the original run. Dispatch the
same workflow with `resume_run_id` and `resume_pr` identifying that original
candidate. Recovery checks the original successful compiled-verifier job,
reconstructs all source from its original base, verifies every current PR
file/head and the real CI receipts, and creates only missing matching release
objects. It never reruns a paid call or overwrites a differing release.

An ordinary subsequent poll refuses to overwrite an existing proposal. If
main advanced before an unmerged proposal passed, a maintainer must close the
stale proposal and explicitly prepare a fresh verified candidate; no hidden
force-push or automatic safety-policy rewrite occurs. A release requiring a
semantic protection port also remains blocked for review.

Local checks (no network, compiler or provider account required):

```sh
TMPDIR="$HOME/t" python3 -W error::ResourceWarning -m unittest discover \
  -s scripts/codex-update -p 'test_*.py' -v
python3 scripts/check_workflow_action_pins.py
python3 scripts/codex-update/update.py identity
```

Official behavior references: [workflow triggers and bot approval](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[eligible required-check events](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks),
[workflow-run approval API](https://docs.github.com/en/rest/actions/workflow-runs#approve-a-workflow-run-for-a-fork-pull-request),
and [scoped job-token permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).
