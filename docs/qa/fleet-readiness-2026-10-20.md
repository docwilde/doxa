# Fleet acceptance on 2026-10-20

This record separates the installed **2.0.0-beta.63 / c2f80bed** observations
from source fixes and later private candidate binaries. The trials use new,
consented, controlled pilot tasks in disposable repositories. They are neither
natural project work nor independently human-labelled supervision benchmarks.
No existing owner conversation was prompted, stopped or reconfigured.

All containers use the reviewed rootless Engine and the installed pinned worker
image. Claude/Codex subscription billing was unknown, so the trials used exact
account-advertised GLM API targets and separate DeepSeek API reviewers. An
explicit Router pin avoids a decision-service request. Local test fixtures are
listed separately from real provider observations.

## Observed behavior

| Check | Result and scope |
| --- | --- |
| Default role isolation | Actual coordinator and worker containers use `network=none`, read-only roots, `cap-drop=ALL`, `no-new-privileges`, and no Docker socket mount. GLM/DeepSeek API requests run on the host; this does not exercise the Claude/Codex provider proxy. |
| Missing independent reviewer | Installed beta.63 starts the real Docker slots but holds initial dispatch, displays `unavailable`, and reserves no reviewer spend when the selected API credential is absent. |
| Unknown paid review | Installed beta.63's first real DeepSeek review failed without verified usage; its $0.007322 reservation remains retained. No worker task was dispatched and no retry was attempted. |
| Real reviewer and peer message | With bounded reviewer requests, real GLM worker/coordinator turns and typed status delivery succeeded. DeepSeek supplied all five message scores, admission was recorded, and the waiting dependent received no task. |
| Start Fleet menu | The source candidate offers Router engine selections, exact target IDs and a private config path. A real GTK/VTE keyboard run edited an explicit DeepSeek reviewer, read all review pages, armed and confirmed launch, observed a successful GLM turn, then stopped the new fleet. This is a real provider run, separate from the earlier review-only terminal probe. |
| Real menu accounting | One worker turn reported 1,236 input / 3 output tokens and $0.000187 at the declared worker rates. Its independent review reported known usage, $0.001852 estimated cost, and a retained $0.004460 conservative reservation. The verdict was `unavailable`; known token usage does not imply a valid alignment verdict. |
| Controller restart | Actual SIGKILL/resume preserved the exact slot session IDs and did not redeliver task prompts. Further event analysis exposed replayed completion observations; see the remaining work below. |
| Idle network migration | The original post-restart transition failed because recovered slots had an empty project scope. After retaining the verified registry session, actual supervisor-only offline-to-online migration succeeded; the worker remained offline and independent supervision remained paused for owner review. |
| Handoff failure | Two real attempts stopped safely on a failed worker handoff turn and retained their unknown reservations. The second attempt's new typed diagnostic is `tool_failed`, not a request/output limit or HTTP category. The old generic failure cannot be retroactively assigned a cause. |
| Teardown | Every completed probe/pilot reported `finished`, `live=false`, and absent daemon sockets. The private journals and unknown reservations remain available for inspection. |

The live message review is a feasibility observation, not a calibrated quality
claim. Human-labelled holdout requirements are unchanged. Controlled pilot
messages can enter a blinded owner-review packet only with their actual charter,
assignment, message hash, verdict and latency bound together.

## Source corrections exercised

The Start Fleet form now forwards Router pins/configuration through the existing
reviewed launch path. Enter edits an exact alignment reviewer selector, while
Left/Right retain the existing presets. Neither editing nor opening the review
grants native execution or internet access.

Controller recovery retains the observed registry session instead of fabricating
an empty project scope. The existing ID, socket, hello, model, working directory
and charter checks still apply before an isolation change.

Worker failures now retain closed diagnostic categories on the error card and
Router receipt. Request-byte, output-token and tool-step limits are distinct;
HTTP errors use fixed categories. Raw provider error bodies, codes, credentials
and endpoints are not included. Unknown usage still retains the full reservation.

Malformed arguments to a known peer tool return a scrubbed, visible model error
without calling a host handler, requesting permission or taking action. This
allows a bounded model turn to correct its arguments. Mandatory scrubbing and
canonical LORE/backend failures retain their uncertain-outcome boundary.

## Reproduce and inspect privately

The host-specific harnesses are under `scripts/fleet-validation/`:

- `runtime_probe.py` uses real installed daemons, rootless containers and a
  deliberately unavailable independent reviewer. Its default mode makes no
  generation calls. `--native-menu` adds a private GTK/VTE review-only probe.
- `--native-menu-live` explicitly enables the separate $0.10 maximum menu trial:
  $0.08 for one pinned GLM API worker and $0.02 for stateless DeepSeek review.
  It confirms a real launch and requires a known-usage successful turn before
  stopping the fleet. The private receipt records both successes and failures.
- `live_pilot.py` creates a separate supervised dependency case. It validates the
  worst-case 25-request worker reservation against each disjoint slot allocation
  before launch. Prior retained holds must be deducted explicitly. It stops on
  unknown usage and never retries that call or clears its journal.

Use a fresh short real-disk `--state` path, owner-private permissions and the
reviewed rootless Engine. The harnesses set explicit private LORE transcript
roots as well as a private memory root. Provider credentials are read privately
and passed through the child environment; they are absent from argv, reports
and repository files. Native terminal captures can contain private account
labels and must not be published as gallery assets.

Published rates were checked against the official
[GLM pricing page](https://docs.z.ai/guides/overview/pricing) and the local exact
model registry. Router request-byte/output limits and durable reservations bound
model work. Token-based costs remain estimates, not account invoices. Unknown
or failed attempts remain charged against the conservative pilot allocation.

## Remaining acceptance

Full accepted handoff → ACK/read-back → sender confirmation → explicit owner
release → dependent dispatch has not yet passed this live record. Research
request/response after supervisor-only network approval also remains to be
recorded. No configured API fetch tool was added merely for this trial.

Restart event accounting needs to retain processed event cursors: the original
resume path replays the retained ring and can count old `turn_done` observations
again. A separate issue to examine is the handoff release's exact successor-turn
binding when an ACK starts an automatic peer turn. Neither constraint should be
weakened to make an acceptance run pass.

Local focused validation passed: four fleet-form tests, four isolation/recovery
checks, 21 vendor transport tests, the closed diagnostic/redaction test, and the
malformed-peer no-handler/no-approval regression. Broader integration CI belongs
to the final combined change and is not implied by these focused results.
