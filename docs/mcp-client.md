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
one link and no final symlink, at most 256 KiB. Duplicate/unknown config fields
fail closed. Config never comes from project discovery. `schema_version` is 1.
Every server has an explicit ID, enabled flag, exact tool allowlist, transport,
and bounded limits. The [search templates](examples/mcp-search.json) are disabled.

An environment reference is `{ "env": "EXA_API_KEY", "bearer": false }`.
For an HTTP bearer header, `bearer: true` adds the prefix locally. Header values
are never config literals. URLs cannot contain userinfo, a query, or fragment;
do not put credentials in their paths. Stdio starts an absolute, already
installed program with only explicitly referenced environment variables.
Package launchers which install on connection, such as npm/npx, are rejected.
Arguments are public configuration and must not contain referenced credentials.

Defaults are a 30-second request/list deadline, 256 KiB frame/request/HTTP-stream
limit, 1 MiB aggregate catalog, 16 pages, 128 advertised tools, 64 intervening
server messages, and 500 ms per cleanup stage. Validation places finite upper
bounds on each setting. Progress does not reset a deadline. Cancellation uses
the provided `Arc<AtomicBool>`; attempted ordinary requests receive a bounded
cancellation notification, then the connection is closed. Initialization is
never cancelled by notification.

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
Known connection credential values are scrubbed from returned strings;
reflection in object keys is refused. Media/resource content is returned as
untrusted data, never automatically opened or fetched. The client checks schema
and result shapes, not full JSON Schema validation. See the official
[tools contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

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
