# GLM account reporting

The GLM engine uses the standard Z.ai API at `https://api.z.ai/api/paas/v4/chat/completions`. Account reporting does not change that endpoint, grant Coding Plan entitlement, or admit a model turn from account quota.

## Settings and display

In `/settings` → **Session**, choose **GLM account reporting**:

- `standard_api` is the default.
- `api_resource_bundle` declares that the owner purchased an API resource bundle. DOXA labels this **owner-declared**, without inferring it from an API key or model catalog.

These values are stored as `glm_account_profile`. `DOXA_GLM_ACCOUNT_PROFILE` overrides the setting and makes the Settings row read-only. They do not alter the active model or session allowance.

The GLM account chip opens `/usage`. Cash balance, API resource bundle remainder, and actual provider charges are **unknown**: the official interfaces reviewed on 2026-10-11 did not establish a standard API balance or resource remainder API. The session cost remains a separate estimate from the model registry's dated public token prices. It is not a receipt for a prepaid package deduction. Check the [Z.ai billing console](https://z.ai/manage-apikey/billing) for account billing; Z.ai documents delayed billing/history reporting in its [API FAQ](https://docs.z.ai/help/faq).

## Separate external Coding Plan monitor

**External Coding Plan monitor** is a separate default-off Settings switch (`glm_plan_monitor`, overridden by `DOXA_GLM_PLAN_MONITOR`). Enable it only if an external plan quota report is useful. Open `/usage`, or the GLM account chip, and press **R** to request a read-only refresh. It reuses the GLM credential through the existing secure credential resolver. No key is put in the settings file or displayed.

This monitor makes no inference call and does not switch the session to a Coding Plan endpoint. Purchasing API credits, declaring a bundle, having a key, or receiving quota data does not prove plan entitlement for DOXA. The [Coding Plan FAQ](https://docs.z.ai/devpack/faq) and [supported tools guide](https://docs.z.ai/devpack/tool/others) describe a separate product and supported tool restrictions.

The quota contract comes from Z.ai's official [usage-query plugin script](https://github.com/zai-org/zai-coding-plugins/blob/0446d0bb0bc537d97d3ab3664c4b8b9c4a0e1254/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs), pinned to commit `0446d0bb0bc537d97d3ab3664c4b8b9c4a0e1254`, reviewed 2026-10-11:

- One GET to `https://api.z.ai/api/monitor/usage/quota/limit`, with the key directly in `Authorization`.
- Three-second overall deadline, no retry, no proxy, no redirect, 16 KiB response cap, and at most eight unique quota type codes. Duplicate codes or malformed percentages/types are refused.
- Only finite provider-returned percentages in `0..=100` are displayed. The report has an observation timestamp. After 120 seconds, percentages are hidden as stale until refreshed.
- No inferred token/credit amounts, cash remainder, quota duration or account tier. Unknown bounded provider quota type codes are shown as generic provider quotas.
- Missing credentials, unauthorized, unavailable, timeout, malformed or oversized results remain unknown. Provider error bodies, keys and arbitrary account text are withheld.

Startup, account catalog checks, model turns, and idle status polling never issue the monitor GET. `/usage` **R** invokes the native `refresh_billing` RPC, which starts a bounded background refresh; subsequent status polling reads its cache. Unavailable monitor data does not block a turn. Stop/interrupt cancels an active monitor without waiting for provider quota data; the transport checks cancellation every 25 milliseconds, within its overall deadline. Closing `/usage` leaves the bounded read-only refresh running. Cached data is not persisted across a daemon restart, and requesting a model catalog invalidates the quota cache so credentials can be changed safely.

This first reporting slice covers direct native GLM sessions. Router tracked Jev/API costs retain their separate existing accounting scope; no GLM account entitlement is inferred for routed targets. The loopback daemon fixture deliberately suppresses external monitor traffic; quota transport tests use only local fake responses.

## Verification scope

Focused fixtures cover valid percentages, false-success/malformed replies, unauthorized, timeout, declared and streamed body caps, redirect refusal, stale cache, profile persistence/env precedence, independent Settings controls, explicit refresh routing and owner targeting, and usage display restoration. A native daemon process fixture covers startup/status unknown data and default-off versus opt-in explicit refresh without model/catalog or external monitor HTTP. No live owner credentials, quota probes, inference calls or owner configuration writes are part of verification. The isolated beta47 lane passed six quota transport fixtures, seven focused GLM UI/bridge/settings checks, and the native daemon startup/status/explicit-refresh fixture (both monitor settings).
