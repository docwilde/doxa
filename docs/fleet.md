# Fleets: N sessions, one prompt, one instant — and more than one machine

Three things landed together, because the experiment in
[`plans/emergent-organization.md`](plans/emergent-organization.md) needs all
three and none of them is useful alone:

1. **`doxa.fleet`** — spawn N sessions, hand them the identical prompt at the
   same instant, wait for quiescence, collect the ledger, tear down.
2. **`doxa.peernet`** — peers on other machines, so several boxes can each run
   a fleet and the fleets can see and message each other.
3. **Memory as a per-agent variable** — `doxa.daemon --no-lore`, because a
   shared LORE store is a coordination channel the message ledger cannot see.

---

## 1. The local fleet harness

```bash
uv run python -m doxa.fleet \
    --prompt "$(cat task.txt)" \
    --pool 'claude:sonnet@8,claude:opus@1,deepseek:deepseek-chat@4' \
    -n 8 --seed 1 --memory-off 2 \
    --root /tmp/dx
```

`--dry-run` prints the capacity arithmetic and the assignment this seed deals,
and spawns nothing. Run it first.

### What makes the start symmetric

The experiment's methodological core is one sentence: *every participant
receives the same task description at the same moment, and none is privileged
in any way.* "The same moment" is not implementable, so here is the honest
approximation, in the order the properties matter:

| | mechanism |
|---|---|
| **Nobody is prompted until everybody is armed** | every session is spawned *and* has a client attached before the first prompt frame is written. Absolute, not approximate: no session can begin work in a world where another does not yet exist. |
| **The text is byte-identical** | one string, handed to every session unmodified. No per-session formatting exists in `FleetRun.dispatch` — "you are agent 7 of 32" would hand a participant its own position, and a position is a privilege. |
| **Dispatch order is randomised per run** | drawn from the run seed, so slot 0 is not systematically first across a cell's five replications. |
| **The residual spread is measured** | `dispatch_spread_s` in the manifest. Observed: **1–11 ms across N = 4…32** on a 16-core laptop. A paper states that number rather than claiming simultaneity. |

### Per-run isolation

Each run gets its **own `DOXA_HOME`** and its **own `DOXA_RUNTIME_DIR`**:

```
<root>/<run-id>/home/peers/messages.jsonl    the run's ledger — the whole file
<root>/<run-id>/rt/registry/                 the run's peer registry
<root>/<run-id>/manifest.json                what the run was, and what happened
```

`doxa.peerledger` puts the ledger at `$DOXA_HOME/peers/messages.jsonl`
deliberately, so a harness collects a run by pointing `DOXA_HOME` at a per-run
directory — no filtering, nothing to separate afterwards. A shared ledger
filtered by time works until two runs overlap, and then it works wrongly and
silently.

The runtime dir is per-run for a different reason: peer *discovery* reads the
registry, so a fleet sharing the machine's registry would discover the
operator's own editor session and count it as a participant. N is a measured
quantity; it has to be the N that was dealt.

> **Keep `--root` short.** An AF_UNIX path is 108 bytes and a per-run directory
> under a per-run root is what spends them. `check_socket_budget` refuses up
> front with the arithmetic rather than letting the bind fail per session,
> minutes into a spawn loop. `/tmp/dx` is a good root; `$HOME/…/pytest-tmp/…`
> is not.

### N, and the arithmetic

```
live CLI process, resident   425–545 MB   (measured)
per session incl. daemon     ~600 MB
N = 32                       ~19 GB
N = 8                        ~4.8 GB
```

`DEFAULT_N` is **4**, not 32. A default is what somebody runs by accident, and
19 GB of new anonymous memory on a 30 GB laptop is a swap storm rather than a
fleet — a swapping fleet does not measure coordination, it measures paging.
`check_capacity` refuses an N that does not fit and names the numbers;
`--force` overrides and is recorded in the manifest.

### Nothing hangs, nothing is left behind

Every phase has a deadline and every deadline has an escalation.

* A session that cannot answer a status call inside two poll intervals is
  marked `hung` and **stops being waited on**. It is still torn down, still
  killed if it has to be, and still named in the manifest.
* Quiescence requires the quiet to *hold* for `--quiet-dwell` seconds. A
  session is idle between its own turn and the turn an arriving peer message
  starts, so the first moment everything is idle is routinely the middle of an
  exchange rather than the end of one.
* Teardown is `stop` → SIGTERM → SIGKILL, then a second pass that asks the OS
  whether the process is *actually* gone. `teardown()` returns the pids that
  survived all of it — normally empty, and loud when it is not.

### Reproducing a run

The manifest carries the seed, the pool, the prompt and its sha256, and the
assignment. `fleet.assign(n, pool, seed=…)` replays the draw exactly.

---

## 2. Memory, per agent

```bash
doxa.daemon --no-lore        # this session only
DOXA_LORE=0                  # this machine's default
```

**Why it is not hygiene.** A LORE store shared by every session is a
communication channel that appears in no ledger. Thirty-two agents reading and
writing one belief store can coordinate through memory instead of through
messages, and the structure the experiment measures *is* the communication
structure — so a hierarchy negotiated through shared memory would be reported
as emergence with nothing to show for it. Per-agent control turns that confound
into a variable: `--memory-off K` runs K of N without memory and asks whether
shared memory substitutes for messaging.

**What off means, strictly:**

* no snapshot in the system prompt, and no per-turn refresh, consult or graph
  block;
* the `lore_*` operators are **absent from the model's tool list** — not
  present and refusing, absent, the way `peer_send` is absent when unarmed;
* no writes: no beliefs, no staged proposals, no session index, no file map, no
  deriver review, no op-log sync.

**What off does not mean.** `lore_core` is still imported and still used:
`scrub_secrets` runs on every received peer frame, every persisted transcript
line and every ledger body, and `project_slug` / `PROJECTS_DIR` derive the
transcript path itself. Nine modules under `doxa/` import it at module level.
The transcript is still written — it is DOXA's own session record, and
`/resume`, `/search`'s local half and the transcript pane read it. Turning
memory off stops DOXA putting anything *into* the store or taking anything
*out* of it; it does not, and cannot today, mean the package is uninstalled.

---

## 3. Peers on another machine

`doxa.peers` finds sessions through a 0700 directory of presence files. Its
security model is the filesystem's — same uid, same machine — which is a good
model and also means two machines cannot see each other at all.

`doxa.peernet` is the bridge across that gap, and it is track R2 of
[`plans/remote.md`](plans/remote.md). **Every authorization question it asks
goes to `doxa.remote_policy`** (track R1, already shipped). There is no second
policy: no allow-list of its own, no opinion about what a remote caller may do,
no opinion about whether listening is on.

### Turning it on

```toml
# ~/.doxa/config.toml
remote_enabled = true
remote_allowed_logins = "you@example.com"
remote_peers = "workstation=ws.tail1234.ts.net:47600"
```

```bash
tailscale serve --bg --https 443 http://127.0.0.1:47600
```

### The four non-negotiables, and where each lives

| rule | where |
|---|---|
| **Loopback is the default.** Nothing binds until `remote_enabled` is on, *and* the bind address is `127.0.0.1` unless changed. Two independent gates. | `peernet.bind_host`, `PeerNetServer.start` → `remote_policy.remote_listening_decision` |
| **No new credential store.** Nothing here reads or writes a token, password or key file. Identity is the `Tailscale-User-Login` header `tailscale serve` attaches; the tailnet vouches for it. An endpoint is a hostname and a name to show — the `Endpoint` dataclass has three fields and none is a secret. | `peernet.Endpoint`, `peernet.request` |
| **The allow-list is DOXA's own.** Empty by default, and an empty list refuses everyone rather than everyone. | `remote_policy.allowed_logins` |
| **Say who is connected.** A peer fetched over the wire carries `PeerInfo.origin` naming the machine, and every surface that shows a local peer shows it — `/peers`, and the `peer_list` tool the model sees. | `peers.PeerInfo.origin`, `ui.labels.peer_origin` |

**The header is believed on one path only.** `tailscale serve` terminates TLS
and forwards to loopback; a header arriving anywhere else was written by
whoever connected. The bridge computes `from_loopback` from the *socket's* peer
address — never from anything in the request — and `identity_decision` refuses
unconditionally when it is false, before it looks at the login at all.

**Origin is established, not received.** `fetch_roster` stamps `origin` from
the endpoint DOXA actually dialled, overwriting whatever the reply said. A
machine cannot return rows claiming to be local. It is the one field in a
`PeerInfo` the reader establishes rather than the writer.

### Three ops, and the request kinds they already are

| op | `remote_policy` kind | why |
|---|---|---|
| `roster` | `read_status` | it is a status read: who is here |
| `history` | `read_transcript` | it returns recorded conversation |
| `deliver` | `send_prompt` | an arriving peer message can *start* a turn, so it spends the receiving machine's budget exactly as a prompt does |

No op maps to `shell_bang` or `set_permission_mode`. `!` shell and
`bypassPermissions` are therefore unreachable from the bridge by construction,
not merely refused by a check. A string this bridge does not recognise is
refused before the policy is consulted at all.

A message that crosses a machine boundary lands on the *same* Unix socket,
through the *same* `PeerHost` receive path, scrubbed by the *same*
`scrub_secrets` call, as one from the session next door. There is no second
delivery path and no second scrub.

---

## Measured

`scripts/fleet_scale.py` runs the real `FleetRun` against real `doxa.daemon`
processes over real Unix sockets with real registry entries, with a scripted
SDK client in place of the Claude CLI — so it measures the *harness's* ceiling
(spawn concurrency, fd pressure, AF_UNIX budget, registry churn, ledger
contention, teardown) rather than the fleet's, which is arithmetic.

On a 16-core / 30 GB laptop with ~11 GB already resident:

| N | wall | dispatch spread | ledger | leaked |
|---|---|---|---|---|
| 4 | 5.0 s | 3 ms | — | 0 |
| 8 | 5.7 s | 1 ms | 8 | 0 |
| 16 | 7.5 s | 8 ms | — | 0 |
| 32 | 12.0 s | 1 ms | 32 | 0 |

See the report in the pull request for what broke first while scaling.
