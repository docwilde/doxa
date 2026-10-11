# Native MCP client

`doxa-mcp` supplies a local, opt-in tools client for API engine hosts. The host
must authorize a configured connection and each tool call. Loading a config
does not start a process, contact a service, install packages, or enable tools.

## Protocol boundary

The client implements the initialize/initialized lifecycle and negotiates
`2025-11-25`, `2025-06-18`, or `2025-03-26`. It disconnects when the server
selects an unsupported version. It advertises no sampling, elicitation,
filesystem roots, resource access, or task execution capability. Server pings
receive an empty reply; other server requests are refused. Initialization
instructions never become host instructions. These rules follow the official
[2025 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).

The later `2026-07-28` HTTP revision removes the initialization/session model
and changes cancellation. That wire format is explicitly unsupported here;
a server must negotiate one of the listed 2025 versions. See the official
[transport revision](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx).

Stdio uses newline-delimited JSON-RPC and a process group. Closing stdin is
followed by bounded waits, TERM, then KILL; descendants are also removed.
Call `close().await` before dropping a turn runtime so its child can be reaped.
Drop additionally kills an outstanding process group as a fallback.

Streamable HTTP sends separate POSTs, accepts JSON and SSE replies, retains an
optional session ID privately, and sends the negotiated protocol header after
initialization. Shutdown attempts bounded DELETE. Redirects and inherited
proxies are disabled. HTTPS is required except for explicitly allowed loopback
HTTP. Session expiry or a disconnected stream is returned to the host; the
client does not replay a tool call. Optional GET notification streams, stream
resumption, legacy HTTP+SSE, and OAuth discovery are not implemented. The exact
HTTP rules are in the official
[2025 transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Private configuration

`Config::load(&absolute_path)` accepts an owner-private regular JSON file with
mode 0600, one link, no symlink in any path component, and a protected owner
directory, at most 256 KiB. File identity is checked around the bounded read. Duplicate/unknown config fields
fail closed. Config never comes from project discovery. `schema_version` is 1.
Every server has an explicit ID, enabled flag, exact tool allowlist, transport,
and bounded limits. The [search templates](examples/mcp-search.json) are disabled.

An environment reference is `{ "env": "EXA_API_KEY", "bearer": false }`.
For an HTTP bearer header, `bearer: true` adds the prefix locally. Header values
are never config literals. URLs cannot contain userinfo, a query, or fragment;
do not put credentials in their paths. Stdio starts an absolute, already
installed program with only explicitly referenced environment variables.
Package launchers which install on connection, such as npm/npx, are rejected.
Arguments are public configuration; known saved/inherited credentials are
refused in executable paths, arguments and endpoints before connection.

Defaults are a 30-second request/list deadline, 256 KiB frame/request/HTTP-stream
limit, 1 MiB aggregate catalog, 16 pages, 128 advertised tools, 64 intervening
server messages, and 500 ms per cleanup stage. Validation places finite upper
bounds on each setting. Progress does not reset a deadline. Cancellation uses
the provided `Arc<AtomicBool>`; attempted ordinary requests receive a bounded
cancellation notification, then the connection is closed. Initialization is
never cancelled by notification.

## MCP Settings

`/settings` has an MCP category using the same title tabs, mouse navigation and
overflow as the other categories. `0` opens MCP; `1` through `9` keep their
existing categories. `mcp_enabled` / `DOXA_MCP_ENABLED` defaults off and
`mcp_config` / `DOXA_MCP_CONFIG` selects the explicit private file. API workers
consume those settings; native Claude/Codex CLI workers retain their own MCP
configuration. Stdio servers execute on the trusted host, outside worker Docker
isolation.

The Tavily, Firecrawl, Exa and Brave Search key rows expose only set/missing
status and source. Enter opens a masked, bounded transient editor; Enter or
Ctrl+S saves the key privately, Esc erases the draft, and Delete removes a saved
key. They never enter ordinary Settings drafts, config.toml, CLI settings list,
or MCP JSON. Key edits cannot be made through command-line arguments.

Search credentials use the existing protected `credentials.json` store and
its workspace/LORE exclusions. `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`,
`EXA_API_KEY` and `BRAVE_API_KEY` take precedence over saved search keys; their
rows are environment-owned and read-only. An invalid environment value or
unsafe store fails closed. The corresponding MCP environment references use a
saved search key when the environment is absent, without changing process-wide
environment variables. Existing native vendor credential precedence is unchanged.

Ctrl+P in MCP explicitly initializes and lists enabled configured servers with
a ten-second overall request deadline plus bounded cleanup. It never calls a
tool or enables MCP. The footer shows each configured server ID, enabled/disabled state, allowlist
count and up to three exact names. PgUp/PgDn pages through servers; Ctrl+R
reloads this local configuration view without connecting. Probe results add
per-server connected/catalog/error status and a config digest snapshot.
No server descriptions, endpoints, headers or key values are shown. Session/pane changes
or a changed config path cancel the probe and withhold stale results. The
config is reloaded and compared before completing a successful snapshot.

## Host integration

```rust,ignore
let config = doxa_mcp::Config::load(path)?;
for server in config.enabled_servers() {
    let mut client = doxa_mcp::Client::connect(server, cwd, cancel.clone()).await?;
    let tools = client.list_tools(cancel.clone()).await?;
    // Host freezes a server-qualified name -> (server ID, exact tool name)
    // mapping, treats schemas as untrusted, and authorizes the selected call.
    let result = client.call_tool(canonical_name, &arguments, cancel.clone()).await;
    client.close().await;
}
```

`Tool` has `name`, optional `description`, `input_schema`, and optional
`output_schema`. The client exposes only allowlisted names actually advertised
in a fully validated paginated catalog. It rejects duplicate names and cursor
cycles. An incomplete refresh disables the old catalog. Tool annotations are
not permission evidence and are not exposed by this client. The host owns
provider-safe qualified names and the closed provenance mapping; never split an
arbitrary model-supplied name to select a server.

`CallResult` retains bounded `content`, optional `structured_content`, and
`is_error`. A tool-level error is a result, distinct from a JSON-RPC failure.
All saved/inherited vendor, Jev and search keys plus connection credentials
are scrubbed from returned strings in raw and common URL-encoded forms;
reflection in object keys is refused. Arbitrary secret transformations by an
external server are outside this exact-value redaction contract. Media/resource content is returned as
untrusted data, never automatically opened or fetched. The client checks schema
and result shapes, not full JSON Schema validation. See the official
[tools contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

Keep a session runtime alive until `close().await` has completed. A per-turn
runtime must not own clients intended for later turns. `connect_bounded` caps
each initialize/initialized phase while preserving configured list/call limits;
the host owns its aggregate preparation deadline. `credentials_current()`
re-resolves the exact selected references and compares with the frozen
connection credentials without exposing values or hashes. `call_tool` refuses
changed/missing credentials before sending; it never refreshes authentication.
The host must likewise revalidate the selected private configuration digest
immediately before a reviewed call.

`Error { kind, attempted }` contains safe categories and numeric RPC/HTTP codes,
not server error text, URL contents, credentials, or stderr. `attempted` records
a conservative send boundary. A failed/cancelled call after that boundary may
have run remotely and may have incurred service charges. There is no automatic
retry. This client does not measure provider balances or bill tool execution.

## Search connection recipes

These templates are operator-reviewed starting configurations, not live
availability or authentication results. Copy to a private file, replace the
Brave installed script path, review the exact tool names/allowlists, then enable
only chosen servers. Actual tools depend on the provider's advertised catalog.

| Provider | Template transport/auth | Official connection source |
| --- | --- | --- |
| Brave Search | Installed Node stdio build, `BRAVE_API_KEY`, `--transport stdio`, allow `brave_web_search` | [Brave's server](https://github.com/brave/brave-search-mcp-server/blob/main/README.md) |
| Tavily | `https://mcp.tavily.com/mcp/`, `Authorization: Bearer` from `TAVILY_API_KEY`, allow `tavily-search` | [Tavily's README](https://github.com/tavily-ai/tavily-mcp#remote-mcp-server), [docs](https://docs.tavily.com/documentation/mcp) |
| Firecrawl | `https://mcp.firecrawl.dev/v2/mcp`, `Authorization: Bearer` from `FIRECRAWL_API_KEY`, allow `firecrawl_search` | [Firecrawl API-key recipe](https://docs.firecrawl.dev/mcp-server/keyless#add-an-api-key) |
| Exa | `https://mcp.exa.ai/mcp`, `x-api-key` from `EXA_API_KEY`, allow `web_search_exa` | [Exa authentication](https://exa.ai/docs/get-started/exa-mcp#authentication) |

Connection/list probes can authenticate and inspect a catalog; they do not call
search, crawl, research, or worker-model tools. No live connections or paid
calls are part of the local deterministic regression suite.
