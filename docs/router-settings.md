# Model Router in Settings

Open `/settings` → **Session**.

1. Set **Model Router config** to an absolute path to a private, owner-owned
   candidate JSON file. Define exact worker models, efforts, caps and fallback.
2. Select **routing decision model**: use the config value, **Jev 1.13.0** or
   **SubQ Snap Preview**. Both are decision services; worker choices remain in
   the candidate file.
3. Enter **Jev API key** or **SubQ API key** in its masked editor. Enter saves
   privately; Delete removes the saved key. `TYPESAFE_API_KEY` / `SUBQ_API_KEY`
   environment values take precedence and make that key read-only.
4. Press **Ctrl+P** on the engine, config or routing-model row. The review reads
   the exact effective config, selected key availability and worker catalogs.
   It submits no decision or worker inference. Key presence does not prove
   authentication or available credits.
   Saved service keys use owner-private `<DOXA_HOME>/decision-credentials.json`
   with file mode `0600`.
5. Choose **Model Router (on for new sessions)** as default engine, then
   **Ctrl+S** after the review is ready. Choose a fixed engine to disable it.

The routing-model setting applies to new sessions. Existing sessions retain
their frozen decision model and candidate configuration. A change to the
selected model invalidates an earlier review; the host verifies the selected
service, target, context, tools, permissions and budget before a turn.

Router target dropdowns offer **Auto** and the available configured target IDs.
An unavailable saved pin remains visible and requires explicit correction.
**R** refreshes; reviews expire after 60 seconds. Missing, failed or empty
catalogs remain unverified. Environment-owned settings remain read-only.

Jev costs $0.042/M input tokens; Snap costs $0.05/M input tokens, with free
output. Reservations include failed/unreported attempts. CLI worker account
charges are unknown and excluded from the tracked routing/direct API allowance.
Authentication does not establish subscription billing or a free worker.
[Wire contract and accounting](jev-router.md).

Open `/settings` → **Fleet** to select either pinned service independently for
**independent supervisor model** and **fast message judge model**. Their dropdowns
also offer **Custom model…** for existing API reviewer providers. Review budget,
call ceiling, shadow/enforce and human approval controls remain separate.
[Independent supervision](fleet-supervision.md).

Routing **Auto** chooses a worker at turn boundaries. Command permission
**Auto** remains a separate owner-selected control. Router sessions currently
require native isolation. Selection never grants additional tool permissions.
