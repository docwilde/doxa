# Jev Router in Settings

Open `/settings` → **Session**. The first two rows are **default engine** and
**Jev Router config**.

1. Enter the absolute path to your reviewed Router JSON file. It must be an
   owner-private regular file, with a valid current Router schema.
2. Select either Router row and press **Ctrl+P**. This reads the configuration
   and current provider account catalogs. It sends no Jev decision, user turn,
   tool request, or worker inference. The candidate file is never edited.
3. Select **Jev Router (on for new sessions)** as the default engine. Once the
   review has an available exact fallback and a validly shaped Jev key is
   present, **Ctrl+S** saves. The key's presence does not verify Jev authentication.
4. To turn routing off for new sessions, select a fixed default engine and save.
   Existing sessions retain their engine and routing configuration.

The review distinguishes available, unavailable, and unknown targets. Exact
model and effort must appear in the connected account catalog; static model
names and a successful executable lookup are insufficient. Direct API targets
also require credentials and source-dated conservative price bounds. A failed
or empty catalog never becomes a checked target. Environment-owned settings
remain read-only. Unrelated settings can still be saved if an inherited Router
config is unavailable.

New-session Router target choices reuse the review and list **Auto** plus the
available configured target IDs. An unavailable saved pin remains visible as
the current selection and must be corrected explicitly. **R** refreshes the
review. Reviews expire after 60 seconds or a config change; the host rechecks
account, context, tool capability, permissions, and allowance before work.

`cli_account` targets use the authenticated Codex or Claude CLI account. Their
charges are unknown and excluded from the tracked Jev/direct API allowance.
Mixed Router cost chips label the tracked amount **Jev/API · CLI ?**; usage and
routing details state that total spend is unknown. This scope persists through
later direct API turns and reattach. Authentication is not evidence of a
subscription or a zero-dollar worker price.

Routing **Auto** chooses the worker at turn boundaries. Command permission
**Auto** is a separate owner-selected permission mode. Router sessions currently
require native isolation. API and CLI workers retain their own protected tool
authority; Router selection does not grant additional permissions.
