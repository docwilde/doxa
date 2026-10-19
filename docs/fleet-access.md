# Fleet access and isolation

New fleets default to **docker-offline for both workers and the acting
coordinator**, independently of the ordinary new-session preference. There is no
fallback to native execution when Docker is missing. Docker requires the existing
reviewed Linux rootless Engine and pinned worker image.

Open **Start Fleet** from the command palette, or press **N** in the Fleet
browser. The form collects the task, engines/models, worker count, coordinator,
isolation profiles, budgets and independent review settings. F10 opens the
existing read-through launch review. Read every row, Shift+A to arm, then Shift+Y
to launch. The review names each role's network access and warns explicitly about
native host/credential access. Merely editing the form grants no access.

For the CLI, choose each role independently:

```sh
doxa fleet start --pool glm -n 3 --supervisor glm --prompt 'Your task' \
  --run-budget 5 --supervisor-isolation docker-open --allow-supervisor-internet
```

Workers remain offline. They send `peer_send` questions containing the research
question/URL and purpose to the coordinator. Any coordinator with owner-approved
internet access can assess and fulfil these requests and return evidence with
source URLs. Docker isolation does not disqualify a coordinator. An offline or
absent coordinator must escalate to the owner. This uses the existing peer
question/evidence channel; it does not auto-grant network access or execute a
worker-supplied command. Independent alignment classifiers are separate stateless,
read-only API calls with no tools, not research coordinators.

`--worker-isolation docker-open` additionally requires `--allow-worker-internet`.
Native workers require **both** `--confirm-native-workers` and
`--allow-worker-internet`; native coordinators similarly require
`--confirm-native-supervisor` and `--allow-supervisor-internet`. Native execution
inherently exposes host networking; it cannot provide an offline guarantee.
Legacy `--isolation PROFILE` sets both roles and retains these confirmations.
`--dry-run` reports required acknowledgements without launching anything.

Offline provider processes additionally disable Claude WebSearch/WebFetch and
[Codex built-in web search](https://learn.chatgpt.com/docs/config-file/config-basic).
**Ordinary Claude/Codex CLI cannot reach their model provider inside offline
Docker.** Such launches are refused before creating a run. Use API-backed
GLM/DeepSeek (model requests run on the host; workspace tools run in Docker), or
explicitly provision the existing ROOT/docker-hardened provider boundary. Router
CLI candidates have the same connectivity limitation. DOXA never silently opens
worker egress to fix provider connectivity. Built-in web tools disabled when a
provider process starts stay disabled until that provider is restarted; an online
coordinator can still research through its approved network tools.

During a run, attach a slot, click its isolation chip, choose **1** offline Docker,
**2** Docker with internet, or **3** native, and confirm with Shift+Y. The displayed
confirmation is bound to that session. All fleet slots must be idle, with no
queued prompts or pending approvals. Admission is held while the host checkpoints,
migrates and verifies the original session/conversation/workspace. Only the
selected host PID and working directory may change in the frozen assignments.
Every host must accept the exact replacement; stale contexts fail closed.
Supervised fleets stay paused until the owner reviews and continues them.
Dependency fleets retain Docker; frozen offline test recipes retain offline
workers. Existing ROOT migration restrictions still apply.

The CLI equivalent queues a request for the running controller:

```sh
doxa fleet isolation RUN SLOT docker-open --allow-internet
doxa fleet isolation RUN SLOT native --confirm-native --allow-internet
```

Inspect `fleet status RUN` for the request result and each slot's actual profile.
Incomplete transactions stop the fleet and block observation-only resume; inspect
or stop the run rather than replaying an uncertain migration. Requests are owner
files outside container mounts, never peer approvals.

In the Fleet browser, **M** toggles **Messages & alignment**. It shows retained
per-message admission outcomes and all five semantic scores, plus independent
supervisor verdicts, charter clauses, reasons, evidence IDs and recommended
actions. Use PageUp/PageDown to browse. `doxa fleet alignment RUN` exposes the same
read-only view. Classifications are fallible signals, not proof of alignment.
History is bounded to 256 observations and eight recent message bodies; missing
history and unavailable verdicts are labelled rather than treated as safe.
