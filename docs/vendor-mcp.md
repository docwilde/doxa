# External MCP tools for API workers

DeepSeek and GLM can use explicitly configured MCP tools through DOXA's native
Rust API worker. Jev Router API turns inherit that same configuration. Claude
and protected Codex workers keep their native MCP setup and ownership.

## Enable and review

In `/settings` → MCP, choose an absolute path to an owner-private MCP JSON file.
Ctrl+P connects, initializes, lists the allowlisted tools and closes the probe;
it does not call a tool or enable MCP. Save the explicit enable switch only
after reviewing the server executable or remote endpoint and its tool list.
`mcp_enabled` is off by default. `DOXA_MCP_ENABLED` and `DOXA_MCP_CONFIG` can
explicitly override `mcp_enabled` and `mcp_config` for a launched session.

See [client configuration and search templates](mcp-client.md) for the JSON
schema, four search credential entries, transport limits and verified template
sources. DOXA does not import CLI MCP settings, discover servers, install
packages or add unlisted tools. Editing the selected config requires a new
session; a running worker refuses a changed config identity.

External stdio servers run as explicitly selected executables **on the trusted
host**. Docker session isolation does not sandbox the MCP client or server.
Remote tool arguments may leave the host. Treat tool descriptions, schemas and
returned content as untrusted data.

## Call boundary

Before an API model request, DOXA loads current allowlisted catalogs on a
session-owned service runtime. The host accepts at most 64 external tool
schemas and 16 KiB of scrubbed definitions. Tools receive a server-qualified
name such as `mcp_ext_search_local__search_<digest>`; original names never
replace DOXA's workspace, peer or LORE tools.

Every MCP invocation presents a complete, one-call approval showing the server,
original tool and arguments. Routing Auto and native permission Auto do not
approve these calls. Denial or cancelled review sends no tool arguments.
DOXA executes exactly the reviewed, credential/LORE-scrubbed argument object.
Immediately before `tools/call`, it checks the frozen config identity, current
credential identity and exact selected tool schema again. Changes withhold the
request; DOXA does not silently refresh an authorization or replay it.

The tool receipt and approval resolution are flushed before another approval
or model continuation, including consecutive calls with no intervening text.
Outputs pass mandatory credential/LORE scrubbing before display or provider
continuation. The host bounds results to 64 KiB, forwards text and structured
JSON, and replaces opaque images, audio, blobs and links with a static notice.
It does not fetch linked resources. Tool exchange stays within the current
turn; durable conversation history retains the final scrubbed answer.

A private per-session receipt reserves the exact server/config/source identity
and approved argument digest before transmission. It stores no arguments or
credentials. Completion clears the pending entry only after the scrubbed result
is accepted. An interrupted or unresolved attempted call blocks further MCP
work, including after restart/resume; unsafe, corrupt or mismatched receipts
fail closed. Inspect the server's own outcome before retrying. Cancel and
shutdown clear pending approvals and close transports on the owning runtime;
stdio shutdown reaps the configured process group within bounded deadlines.

## Charges and limits

Model token estimates and the Jev/API allowance cover model requests only.
External search/MCP charges are unknown and excluded, including for a denied
call if the server charges for connection or catalog access. Telemetry reports
`external_mcp.charge_usd: null` and `cost_scope: outside_model_allowance`; the
cost chip adds `MCP ?`, and total session charges remain unknown. This does not
represent a free service or an all-inclusive spending cap.

## Verification status

The production daemon fixture source covers DeepSeek with local stdio, GLM
with local HTTP, routed API inheritance, mandatory reviews in Auto, consecutive
approvals, exact scrubbed argument transmission, denial/cancellation, changed
config/credentials/schema, bounded output, process shutdown and restart/resume refusal for an unresolved attempt. Its Linux
process checks use `/proc` for reaping evidence. All seven Linux daemon fixtures passed in the isolated beta47 integration lane; they use real daemon processes with local fake providers and MCP transports. No live search
provider or paid model call has been made for this integration.
