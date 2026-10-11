use doxa_mcp::{CallResult, Client, Config, EnvRef, ErrorKind, Limits, ServerConfig, TransportConfig};
use serde_json::{json,Value};
use std::{collections::BTreeMap, os::unix::fs::PermissionsExt, path::Path, sync::{Arc,Mutex, atomic::{AtomicBool,Ordering}}, time::Duration};
use tokio::{io::{AsyncReadExt,AsyncWriteExt}, net::TcpListener};

static ENV: Mutex<()> = Mutex::new(());
fn cancel() -> Arc<AtomicBool> { Arc::new(AtomicBool::new(false)) }
fn stdio_config(mode: &str, journal: &Path) -> ServerConfig {
    ServerConfig { id:"fixture".into(), enabled:true, allow_tools:vec!["search".into(),"lookup".into()],
        limits:Limits { timeout_ms:2000,shutdown_ms:30,..Limits::default() },
        transport:TransportConfig::Stdio { program:"/usr/bin/python3".into(),
            args:vec!["-u".into(),format!("{}/tests/fake_stdio.py",env!("CARGO_MANIFEST_DIR")),mode.into(),journal.display().to_string()],env:BTreeMap::new() } }
}
fn journal(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path).unwrap_or_default().lines().map(|line|serde_json::from_str(line).unwrap()).collect()
}
fn dead(pid: u64) -> bool {
    // A killed orphan may remain a zombie until the system reaper runs; it is
    // incapable of retaining pipes or executing further work.
    let state = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok();
    state.is_none() || state.is_some_and(|v|v.rsplit_once(") ").is_some_and(|(_,v)|v.starts_with('Z')))
}

#[tokio::test]
async fn stdio_negotiates_paginates_filters_and_returns_structured_tool_errors() {
    for mode in ["good","messages","tool_error","rpc_error"] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("journal.jsonl");
        let mut client = Client::connect(&stdio_config(mode,&path),directory.path(),cancel()).await.unwrap();
        assert_eq!(client.info().protocol_version,"2025-06-18");
        let tools = client.list_tools(cancel()).await.unwrap();
        assert_eq!(tools.iter().map(|t|t.name.as_str()).collect::<Vec<_>>(),vec!["lookup","search"]);
        assert_eq!(client.call_tool("admin",&json!({}),cancel()).await.unwrap_err().kind,ErrorKind::ToolNotAllowed);
        let result = client.call_tool("search",&json!({"query":"synthetic"}),cancel()).await;
        if mode == "rpc_error" { assert_eq!(result.unwrap_err().kind,ErrorKind::Remote{code:-32042}); }
        else { let result:CallResult=result.unwrap(); assert_eq!(result.is_error,mode == "tool_error"); assert!(result.structured_content.is_some()); }
        client.close().await; client.close().await;
        assert!(client.is_closed());
        let records=journal(&path);
        assert_eq!(records.iter().filter(|v|v["method"]=="tools/call").count(),1);
        assert!(dead(records[0]["pid"].as_u64().unwrap()));
    }
}

#[tokio::test]
async fn stdio_inherits_only_explicit_env_and_redacts_result_credentials() {
    let _guard=ENV.lock().unwrap();
    std::env::set_var("DOXA_MCP_FIXTURE_SECRET","synthetic-secret-0824");
    std::env::set_var("DOXA_MCP_UNRELATED","must-not-inherit");
    let directory=tempfile::tempdir().unwrap(); let path=directory.path().join("journal.jsonl");
    let mut config=stdio_config("good",&path);
    if let TransportConfig::Stdio{env,..}=&mut config.transport {
        env.insert("TEST_SECRET".into(),EnvRef{env:"DOXA_MCP_FIXTURE_SECRET".into(),bearer:false});
    }
    let mut client=Client::connect(&config,directory.path(),cancel()).await.unwrap();
    client.list_tools(cancel()).await.unwrap();
    let result=client.call_tool("search",&json!({}),cancel()).await.unwrap();
    let serialized=serde_json::to_string(&result).unwrap();
    assert!(!serialized.contains("synthetic-secret-0824")); assert!(serialized.contains("[redacted]"));
    client.close().await;
    assert_eq!(journal(&path)[0]["unrelated_env_present"],false);
    std::env::remove_var("DOXA_MCP_FIXTURE_SECRET");std::env::remove_var("DOXA_MCP_UNRELATED");
}

#[tokio::test]
async fn initialization_rejects_wrong_protocol_ids_caps_noise_duplicates_and_oversize() {
    for (mode,kind) in [("unsupported",ErrorKind::UnsupportedVersion),("missing_capability",ErrorKind::MissingToolsCapability)] {
        let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
        let result=Client::connect(&stdio_config(mode,&path),directory.path(),cancel()).await;
        assert_eq!(result.err().unwrap().kind,kind);
        assert!(dead(journal(&path)[0]["pid"].as_u64().unwrap()));
    }
    for mode in ["mismatched_id","stdout_noise","duplicate_json","oversize"] {
        let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
        let mut config=stdio_config(mode,&path); config.limits.max_frame_bytes=1024;
        let error=Client::connect(&config,directory.path(),cancel()).await.err().unwrap();
        assert!(matches!(error.kind,ErrorKind::Protocol(_)|ErrorKind::Limit(_)),"{mode}: {error}");
        assert!(dead(journal(&path)[0]["pid"].as_u64().unwrap()));
    }
}

#[tokio::test]
async fn catalog_failures_disable_tools_and_bound_pages_and_notifications() {
    for mode in ["cursor_cycle","duplicate_tool","bad_schema","flood","good"] {
        let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
        let mut config=stdio_config(mode,&path);
        if mode=="flood" {config.limits.max_notifications=2;}
        if mode=="good" {config.limits.max_list_pages=1;}
        let mut client=Client::connect(&config,directory.path(),cancel()).await.unwrap();
        assert!(client.list_tools(cancel()).await.is_err(),"{mode}");
        assert_eq!(client.call_tool("search",&json!({}),cancel()).await.unwrap_err().kind,ErrorKind::CatalogChanged);
        client.close().await;
        assert!(!journal(&path).iter().any(|v|v["method"]=="tools/call"));
    }
}

#[tokio::test]
async fn cancellation_before_connect_never_starts_a_process() {
    let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
    let stop=cancel();stop.store(true,Ordering::SeqCst);
    let error=Client::connect(&stdio_config("good",&path),directory.path(),stop).await.err().unwrap();
    assert_eq!(error.kind,ErrorKind::Cancelled);assert!(!error.attempted);assert!(!path.exists());
}

#[tokio::test]
async fn timeout_cancels_attempted_call_and_cleans_up_process_group() {
    let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
    let mut config=stdio_config("timeout",&path);config.limits.timeout_ms=150;
    let mut client=Client::connect(&config,directory.path(),cancel()).await.unwrap();
    client.list_tools(cancel()).await.unwrap();
    let error=client.call_tool("search",&json!({}),cancel()).await.unwrap_err();
    assert_eq!(error.kind,ErrorKind::Timeout);assert!(error.attempted);assert!(client.is_closed());
    let records=journal(&path);assert!(records.iter().any(|v|v["method"]=="notifications/cancelled"));
    for pid in records.iter().filter_map(|v|v["pid"].as_u64().or(v["descendant"].as_u64())) {
        // SIGKILL is asynchronous for descendants which are not our children.
        // Require their termination within a bounded scheduling window.
        let deadline=std::time::Instant::now()+std::time::Duration::from_millis(500);
        while !dead(pid) && std::time::Instant::now()<deadline {
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        }
        assert!(dead(pid),"PID {pid}");
    }
}

#[tokio::test]
async fn initialization_timeout_sends_no_cancel_notification() {
    let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
    let mut config=stdio_config("init_timeout",&path);config.limits.timeout_ms=100;
    let error=Client::connect(&config,directory.path(),cancel()).await.err().unwrap();
    assert_eq!(error.kind,ErrorKind::Timeout);assert!(error.attempted);
    assert!(!journal(&path).iter().any(|v|v["method"]=="notifications/cancelled"));
}

#[test]
fn private_config_rejects_public_symlink_duplicate_and_unknown_fields() {
    let directory=tempfile::tempdir().unwrap();let path=directory.path().join("config.json");
    let config=json!({"schema_version":1,"servers":[serde_json::to_value(stdio_config("good",&directory.path().join("journal"))).unwrap()]});
    std::fs::write(&path,config.to_string()).unwrap();std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(Config::load(&path).unwrap().enabled_servers().count(),1);
    let link=directory.path().join("link");std::os::unix::fs::symlink(&path,&link).unwrap();assert!(Config::load(&link).is_err());
    std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o644)).unwrap();assert!(Config::load(&path).is_err());
    std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
    for invalid in ["{\"schema_version\":1,\"schema_version\":1,\"servers\":[]}","{\"schema_version\":1,\"servers\":[],\"unexpected\":true}"] {
        std::fs::write(&path,invalid).unwrap();assert!(Config::load(&path).is_err());
    }
}

#[test]
fn disabled_search_templates_are_valid_without_credentials_or_installs() {
    let value:Config=serde_json::from_str(include_str!("../../../docs/examples/mcp-search.json")).unwrap();
    value.validate().unwrap();assert_eq!(value.servers.len(),4);assert_eq!(value.enabled_servers().count(),0);
    for url in ["http://remote.example/mcp","https://user:key@example.com/mcp","https://example.com/mcp?api_key=x","https://example.com/mcp#x"] {
        let mut server=value.servers[1].clone();server.transport=TransportConfig::StreamableHttp{url:url.into(),headers:BTreeMap::new(),allow_loopback_http:true};assert!(server.validate().is_err());
    }
    let mut server=value.servers[0].clone();
    if let TransportConfig::Stdio{program,..}=&mut server.transport {*program="/usr/bin/npx".into();}
    assert!(server.validate().is_err());
}

async fn http_fixture(mode: &'static str) -> (String,Arc<Mutex<Vec<Value>>>,tokio::task::JoinHandle<()>) {
    let listener=TcpListener::bind("127.0.0.1:0").await.unwrap();let address=listener.local_addr().unwrap();
    let records=Arc::new(Mutex::new(Vec::new()));let observed=records.clone();
    let task=tokio::spawn(async move {
        loop {
            let (mut stream,_)=listener.accept().await.unwrap();
            let mut bytes=Vec::new();let mut chunk=[0;4096];
            let (header_end,length)=loop {
                let count=stream.read(&mut chunk).await.unwrap();if count==0 {return;}bytes.extend_from_slice(&chunk[..count]);
                if let Some(end)=bytes.windows(4).position(|v|v==b"\r\n\r\n") {
                    let headers=String::from_utf8_lossy(&bytes[..end]);
                    let length=headers.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length:").map(|v|v.trim().parse::<usize>().unwrap())).unwrap_or(0);
                    break (end+4,length);
                }
            };
            while bytes.len()<header_end+length {let count=stream.read(&mut chunk).await.unwrap();if count==0{return;}bytes.extend_from_slice(&chunk[..count]);}
            let headers=String::from_utf8_lossy(&bytes[..header_end]).to_ascii_lowercase();
            if headers.starts_with("delete ") {
                assert!(headers.contains("mcp-session-id: fixture-session"));
                observed.lock().unwrap().push(json!({"delete":true}));
                stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await.unwrap();break;
            }
            assert!(headers.contains("accept: application/json, text/event-stream"));
            let request:Value=serde_json::from_slice(&bytes[header_end..header_end+length]).unwrap();observed.lock().unwrap().push(request.clone());
            let initializing=request["method"]=="initialize";
            if !initializing {assert!(headers.contains("mcp-session-id: fixture-session"));assert!(headers.contains("mcp-protocol-version: 2025-11-25"));}
            let (status,content_type,body)=if request["method"]=="notifications/initialized" || request["method"]=="notifications/cancelled" {
                ("202 Accepted","application/json",String::new())
            } else if mode=="expired" && request["method"]=="tools/call" {
                ("404 Not Found","application/json","{}".into())
            } else if mode=="redirect" && request["method"]=="tools/list" {
                ("307 Temporary Redirect","application/json",String::new())
            } else {
                let result=match request["method"].as_str().unwrap() {
                    "initialize"=>json!({"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"http-fixture","version":"1"}}),
                    "tools/list"=>json!({"tools":[{"name":"search","inputSchema":{"type":"object"}}]}),
                    "tools/call"=>json!({"content":[{"type":"text","text":"Local HTTP result"}],"structuredContent":{"local":true}}),
                    method=>panic!("unexpected {method}"),
                };
                let response=json!({"jsonrpc":"2.0","id":request["id"],"result":result}).to_string();
                if mode=="sse" && !initializing {
                    // Chunk boundaries split lines, CRLF and UTF-8 below.
                    ("200 OK","text/event-stream",format!(": local comment\r\nid: prime\r\ndata:\r\n\r\ndata: {{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{{\"text\":\"ΔΟΞΑ\"}}}}\r\n\r\ndata: {response}\r\n\r\n"))
                } else {("200 OK","application/json",response)}
            };
            let extra=if initializing {"Mcp-Session-Id: fixture-session\r\n"} else if mode=="redirect" {"Location: http://127.0.0.1:1/mcp\r\n"} else {""};
            let header=format!("HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\n{extra}Connection: close\r\n\r\n",body.len());
            stream.write_all(header.as_bytes()).await.unwrap();
            if content_type=="text/event-stream" {for chunk in body.as_bytes().chunks(7) {stream.write_all(chunk).await.unwrap();tokio::task::yield_now().await;}}
            else {stream.write_all(body.as_bytes()).await.unwrap();}
        }
    });
    (format!("http://{address}/mcp"),records,task)
}

#[tokio::test]
async fn http_json_and_sse_preserve_session_headers_and_close_without_tool_replay() {
    for mode in ["json","sse","expired","redirect"] {
        let (url,records,task)=http_fixture(mode).await;
        let directory=tempfile::tempdir().unwrap();
        let config=ServerConfig{id:"local".into(),enabled:true,allow_tools:vec!["search".into()],limits:Limits::default(),
            transport:TransportConfig::StreamableHttp{url,headers:BTreeMap::new(),allow_loopback_http:true}};
        let mut client=Client::connect(&config,directory.path(),cancel()).await.unwrap();
        if mode=="redirect" {assert_eq!(client.list_tools(cancel()).await.unwrap_err().kind,ErrorKind::Http{status:307});}
        else {
            client.list_tools(cancel()).await.unwrap();
            let result=client.call_tool("search",&json!({}),cancel()).await;
            if mode=="expired" {assert_eq!(result.unwrap_err().kind,ErrorKind::SessionExpired);}
            else {assert_eq!(result.unwrap().structured_content,Some(json!({"local":true})));}
        }
        client.close().await;
        tokio::time::timeout(Duration::from_secs(2),task).await.unwrap().unwrap();
        assert_eq!(records.lock().unwrap().iter().filter(|v|v["method"]=="tools/call").count(),usize::from(mode!="redirect"));
    }
}
