# Live Codex auto permission verification

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
This verifies an idle same-session transition. Active switching remains
unsupported in Codex; the [Claude verification](live-claude-auto-permissions-2026-10-10.md)
separately passed that active transition.

## Build and receipt

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
Commands exclusively create disposable workspace markers and print synthetic
tokens. Only an exact baseline command approval may be answered, once, after
verifying that the attempted active switch was refused and preserved the card.
An approval in an auto turn is left unanswered and fails the check. The script
requires actual successful turns, exact replies, markers, thread continuity,
saved mode and clean stop. It removes temporary state and prints no account
contents, provider text, command tokens or credentials.

Exit code `1` with `status: partial` deliberately reports the unsupported
pending-command switch. It is not a full pass for active switching. Exit code
`0` requires that switch to pass as well.

Credential-free checks run in CI:

```sh
python3 -W error::ResourceWarning -m unittest discover \
  -s scripts/tests -p test_codex_auto_permissions_verifier.py -v
```

## Scope and remaining work

This verifies ordinary Python commands inside a `workspace-write` sandbox on
one installed Linux build, account and model. It does not test outside-sandbox
execution, all tool kinds, other models, organization restrictions, restart
recovery or the graphical permission picker. Auto retains the sandbox; separate
DOXA peer and LORE reviews remain independent.

The current Codex host declares that permission changes require idle, and the
runtime checks active work and queued prompts before allowing the setter.
The driver applies the selected approval and sandbox policy on `turn/start`.
The documented [`turn/steer` interface](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn)
adds input to an active turn without accepting turn-level policy overrides.
Active switching remains an implementation gap in DOXA; this verification
does not change that behavior or certify a workaround.
