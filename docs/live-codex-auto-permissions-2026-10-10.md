# Live Codex auto permission verification

**Development build passed on 2026-10-10:** live `on-request` → `auto`
switching with protected Codex 0.156.1 and `gpt-6-astra` at low effort.
The installed beta.42 baseline below remains partial until the daemon and
protected provider are upgraded together.

## Development: switching an active turn

The completed run started at `2026-10-10T20:20:21.775188+00:00`. It used
three synthetic turns, disposable provider state and an existing account login.
No approval answer was supplied in this run.

| Observation | Development daemon and updated private provider |
| --- | --- |
| Baseline command reached a real approval in `on-request` | Passed |
| Selecting `auto` while that exact card was waiting | Applied and acknowledged |
| Waiting card resolved; no manual approval was sent | Passed |
| Command retried in the normal sandbox and produced its marker and exact token | Passed |
| Two subsequent commands ran without approval cards | Passed |
| Same provider thread and saved `auto` mode across all three turns | Passed |
| Write outside the workspace and temporary-directory roots | Blocked: `EROFS` (errno 30); outside marker absent |
| Acknowledged stop and daemon exit | Passed |

DOXA sends the private `turn/settings/update` extension with the exact thread,
turn and `doxaAuto: true`. The provider validates managed requirements and
changes only the live approval policy to `never`; sandbox settings stay fixed.
An outstanding provider escalation is declined and receives instructions to
retry inside the sandbox. Questions and DOXA peer, LORE and spawn reviews
retain their separate answers. Future turns explicitly use the saved Auto mode.

This live transition supports `on-request` → `auto` with a sandbox already in
place. Reverse transitions, full-access transitions and compaction operations
require idle. Older protected providers refuse the live extension and preserve
the waiting request. A missing acknowledgement or failed persistence stops
the affected session; a successful switch preserves its incomplete-turn guard.

| Build field | Value |
| --- | --- |
| Development daemon SHA256 | `98bcbba82af9488c9b39ba5a8395273a891bce4b9ac121bc74c3bb90696b5f2e` |
| Protected source commit | `b412ff32c417f855c2b2d1581b77058eed87c84b` |
| Reviewed patch SHA256 | `23bccbb08344fc70d1fd8a482b19814ca7f8edff07d478091b8c703e37a7ec6d` |
| Protected app-server SHA256 | `b5db3d57b31eff908d9588504e4b9bb5e7ec6023da702f7ec7e70c5c6cb03fe2` |
| Protected launcher SHA256 | `6098123c5af95a1081c0ac0ad321b72d727e116d89d2939d6fc5a79fad3800c8` |
| Paired code-mode host SHA256 | `dcf89aa28f308834703ea902f661af38caf83999478336784ee6c906574cade0` |
| Additional compiled contract | `doxa-midturn-auto-v1` |

The installer builds and binds both provider artifacts, checks the compiled
contract and installs them into an immutable private directory. The launcher
pins the new patch digest. A verified upgrade preserves the previous immutable
provider. This proof used a separate install root; production binaries were
unchanged.

Local validation passed 161 checks: four native daemon process tests covering
11 live-control scenarios, three private Codex core tests, 124 engine/runtime
checks, one active-picker TUI check and 29 Python verifier/installer checks.
Two existing opt-in engine tests stayed ignored. The source tests include
managed-policy refusal, stale approval answers, independent inputs, partial JSON
reads, late approval suppression and failed or mismatched checkpoints.

Two earlier development runs passed the actual pending switch and first Auto
command, then failed the new sandbox oracle: it caught `PermissionError` but
missed a read-only mount's `OSError` with errno 30. The completed run accepts
only errno 1, 13 or 30, requires the outside marker to remain absent and verifies
the denial marker and exact token inside the workspace.

## Installed beta.42 baseline

**Partial on 2026-10-10:** installed DOXA `2.0.0-beta.42` on Linux,
with the protected Codex provider and the account default `gpt-6-astra`
at low effort. Auto execution and a switch between turns passed. A switch
while a command approval was pending was rejected.

The check used the existing account login, copied privately into disposable
provider state. It submitted three synthetic turns in one native DOXA daemon
and verified the same saved Codex thread ID after every completed turn.

| Observation | Installed daemon |
| --- | --- |
| Initial mode was `on-request` | Passed |
| Baseline Python command reached a real command approval | Passed |
| Switch to `auto` while that approval was pending | Rejected: requires idle |
| Refusal preserved `on-request`, the exact pending card and the unexecuted command | Passed |
| Harness approved only that exact disposable baseline command once | Passed |
| Baseline card resolved and command completed with its marker and exact token | Passed |
| Same session switched to `auto` between turns | Passed |
| First auto command executed with no approval card or supplied answer | Passed |
| Second auto command executed with no approval card or supplied answer | Passed |
| Provider thread ID remained the same across all three turns | Passed |
| Saved mode after the last turn was `auto` | Passed |
| Stop was acknowledged and the daemon exited | Passed |

The active switch returned:

```text
Finish the current response and queued prompts, then change permissions for the next turn
```

The harness completed that baseline turn using one approval bound to the exact
synthetic command and pending ID. It supplied no answers in either auto turn.
This verifies an idle same-session transition. Active switching was
unsupported in this installed build; the [Claude verification](live-claude-auto-permissions-2026-10-10.md)
separately passed that active transition.

## Installed build and receipt

The completed run started at `2026-10-10T19:33:36.622837+00:00`.

| Field | Value |
| --- | --- |
| Installed daemon | `/home/docwilde/.local/bin/doxa-daemon-rs` |
| Daemon SHA256 | `82fd61a481e1b2c1571096c89cde799e6a774f84cab1006c37fcee55daa0328b` |
| Protected provider contract | `doxa-precompact-fail-closed-v1` |
| Protected provider source commit | `b412ff32c417f855c2b2d1581b77058eed87c84b` |
| Protected app-server SHA256 | `16d9db61f26fb4d5a4a47b6ea465c34e661caa3160d545cda92459ba99e435eb` |
| Protected launcher SHA256 | `857f950c183afb93ce55fdd4e37081fab6c3ca9b2a83ef9da32885bf3344ebad` |

The protected launcher's non-app-server `--version` delegates to the installed
official CLI (`0.162.1`); that string alone does not identify the protected
app-server. The source commit, contract and app-server digest above identify
the provider used here.

Two setup attempts selected the historical `gpt-5.5` model and were rejected
by the live account catalog before any turn was submitted. The completed run
used the current account default; cached model names do not establish live
availability.

The allowlisted receipt reported:

```json
{
  "status": "partial",
  "submitted_turns": 3,
  "initial_on_request": true,
  "auto_commands": "passed",
  "same_session_idle_switch": "passed",
  "pending_command_switch": "unsupported_requires_idle",
  "same_provider_thread": true,
  "auto_mode_persisted": true,
  "stop_exited": true
}
```

## Reproduce

Choose a short owner-private absolute `TMPDIR` outside the checkout and run:

```sh
TMPDIR=/short/private/directory \
python3 scripts/verify_codex_auto_permissions.py --live
```

Defaults use the installed native daemon, protected provider, LORE carrier and
existing account login. `--model` can select an explicit currently available
account model; otherwise the account default is used. Absolute binary and
account paths can be overridden through the script's named options.

The verifier submits at most three turns, each with a 90-second event deadline.
Commands create disposable workspace markers and print synthetic tokens. The
last command also attempts one disposable write outside the workspace and
temporary-directory roots; successful verification requires a sandbox denial. Only an exact baseline command approval may be answered, once, after
verifying that the attempted active switch was refused and preserved the card.
An approval in an auto turn is left unanswered and fails the check. The script
requires actual successful turns, exact replies, markers, thread continuity,
saved mode, the outside-write denial and clean stop. It removes temporary state and prints no account
contents, provider text, command tokens or credentials.

Exit code `1` with `status: partial` deliberately reports the unsupported
pending-command switch. It is not a full pass for active switching. Exit code
`0` requires that switch to pass as well.

Credential-free checks run in CI:

```sh
python3 -W error::ResourceWarning -m unittest discover \
  -s scripts/tests -p test_codex_auto_permissions_verifier.py -v
```

## Scope of the installed baseline

This verifies ordinary Python commands inside a `workspace-write` sandbox on
one installed Linux build, account and model. It does not test outside-sandbox
execution, all tool kinds, other models, organization restrictions, restart
recovery or the graphical permission picker. Auto retains the sandbox; separate
DOXA peer and LORE reviews remain independent.

The installed beta.42 Codex host declares that permission changes require idle, and the
runtime checks active work and queued prompts before allowing the setter.
The driver applies the selected approval and sandbox policy on `turn/start`.
The documented [`turn/steer` interface](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn)
adds input to an active turn without accepting turn-level policy overrides.
That installed build retains the active-switch gap. The development implementation
and separate live proof are recorded above.
