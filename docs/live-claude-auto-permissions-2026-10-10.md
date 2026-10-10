# Live Claude auto permission verification

**Passed on 2026-10-10:** DOXA `2.0.0-beta.42` with Claude Code `2.1.296`
on Linux, using the existing subscription login and the CLI's default model.

The check ran twice: once against the native development daemon and once
against the installed `doxa-daemon-rs` used by the installed launcher.
Each run submitted two synthetic turns in a disposable private workspace.
No human approval or session-wide tool allow rule was supplied.

| Observation | Development daemon | Installed daemon |
| --- | --- | --- |
| Session initially reported `default` permission mode | Passed | Passed |
| First Python command reached a real Bash approval card | Passed | Passed |
| Switching that active turn to `auto` reported the requested mode | Passed | Passed |
| The exact pending card resolved | Passed | Passed |
| First command created its marker and returned the exact token | Passed | Passed |
| A second Python command executed in auto without an approval card | Passed | Passed |
| Both turns completed successfully | Passed | Passed |
| Stop was acknowledged and the daemon exited | Passed | Passed |

Run start times were `2026-10-10T18:43:52.380826+00:00` and
`2026-10-10T18:48:04.936333+00:00`. The installed daemon's SHA256 was
`82fd61a481e1b2c1571096c89cde799e6a774f84cab1006c37fcee55daa0328b`.

## Reproduce

Build or select the native daemon, select a short owner-private absolute
`TMPDIR` outside the checkout, then run:

```sh
DOXA_NATIVE_DAEMON=/absolute/path/to/doxa-daemon-rs \
TMPDIR=/short/private/directory \
python3 scripts/verify_claude_auto_permissions.py --live
```

The verifier requires explicit live opt-in. It exposes only Bash to Claude,
submits at most two turns, and gives each turn a 90-second event deadline.
The commands exclusively create disposable marker files and print synthetic
tokens. The first pending request is left unanswered while mode changes;
fresh provider requests cause verification to fail rather than being approved.
Events arriving before the mode RPC reply remain available for exact-card
verification. Temporary state is removed and daemon descendants are stopped.
Its JSON receipt contains allowlisted observations, without account contents,
raw provider replies, reasoning, command tokens, or credentials.

The credential-free verifier checks are:

```sh
python3 -m unittest discover -s scripts/tests \
  -p test_claude_auto_permissions_verifier.py -v
```

## Scope

This verifies the mid-turn transition and a subsequent Bash call in this
account and CLI environment. It does not establish classifier acceptance for
arbitrary commands, other models, or organization settings. Questions,
explicit ask rules, organization limits, fresh classifier refusals, and the
independent spawn gate remain interactive.

The reported `resolve_reviewed.py` invocation was not executed; that file was
unavailable in this environment. A synthetic `python3 /absolute/path/marker.py`
command exercised the same Bash invocation shape with disposable data.
