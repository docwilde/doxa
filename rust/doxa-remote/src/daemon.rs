//! A small client for the same bounded session wire used by the local TUI.
use doxa_protocol::{self as protocol, Direction};
use serde_json::{json, Value};
use std::{fs::OpenOptions, io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write},
    os::unix::{fs::{MetadataExt, OpenOptionsExt}, net::UnixStream},
    path::Path, time::Duration};

const SNAPSHOT_BYTES: u64 = 8 * 1024 * 1024;
const MAX_TURNS: usize = 40;
const MAX_TEXT: usize = 20_000;

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }

pub struct Client { reader: BufReader<UnixStream>, writer: UnixStream, pending: Vec<u8>, pub hello: Value, next_id: u64 }
impl Client {
    pub fn from_stream(stream: UnixStream, expected_id: &str, login: Option<&str>, cursor: Option<u64>) -> io::Result<Self> {
        stream.set_read_timeout(Some(Duration::from_secs(2)))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let writer = stream.try_clone()?;
        let mut reader = BufReader::new(stream);
        let mut pending = Vec::new();
        let hello = read_frame(&mut reader, &mut pending)?;
        if hello["type"] != "hello" || hello["session_id"] != expected_id { return Err(invalid("session identity changed")); }
        let cursor=cursor.or_else(||hello["next_seq"].as_u64());
        write_frame(&writer, &json!({"type":"attach","cursor":cursor,"remote_login":login}))?;
        Ok(Self { reader, writer, pending, hello, next_id: 1 })
    }
    pub fn next(&mut self) -> io::Result<Value> { read_frame(&mut self.reader, &mut self.pending) }
    pub fn idle_timeout(&self) -> io::Result<()> { self.reader.get_ref().set_read_timeout(Some(Duration::from_secs(15))) }
    pub fn short_timeout(&self) -> io::Result<()> { self.reader.get_ref().set_read_timeout(Some(Duration::from_millis(20))) }
    pub fn remote_prompt(&mut self, text:&str, allow_unrestricted:bool)->io::Result<Value>{
        self.request(json!({"type":"prompt","text":text,"remote":true,"remote_allow_unrestricted":allow_unrestricted}))
    }
    pub fn call(&mut self, method: &str, params: Value) -> io::Result<Value> {
        self.request(json!({"type":"call","method":method,"params":params}))
    }
    fn request(&mut self, mut frame: Value) -> io::Result<Value> {
        let id = self.next_id;
        self.next_id = self.next_id.checked_add(1).ok_or_else(|| invalid("request ids exhausted"))?;
        frame["id"] = json!(id);
        write_frame(&self.writer, &frame)?;
        for _ in 0..1024 {
            let answer = self.next()?;
            if answer["type"] == "reply" && answer["id"] == id { return Ok(answer); }
        }
        Err(invalid("daemon reply did not arrive within event bound"))
    }
}

fn write_frame(mut stream: &UnixStream, frame: &Value) -> io::Result<()> {
    let bytes = protocol::encode_line(frame, Direction::ClientToServer).map_err(io::Error::other)?;
    stream.write_all(&bytes)
}
fn read_frame(reader: &mut BufReader<UnixStream>, line: &mut Vec<u8>) -> io::Result<Value> {
    loop {
        let bytes = reader.fill_buf()?;
        if bytes.is_empty() { return Err(io::ErrorKind::UnexpectedEof.into()); }
        let count = bytes.iter().position(|byte| *byte == b'\n').map_or(bytes.len(), |pos| pos + 1);
        if line.len() + count > protocol::MAX_FRAME_BYTES { return Err(invalid("daemon frame exceeded 64 KiB")); }
        let ended = bytes[count - 1] == b'\n';
        line.extend_from_slice(&bytes[..count]);
        reader.consume(count);
        if ended {
            let result = protocol::decode_line(line, Direction::ServerToClient).map_err(io::Error::other);
            line.clear();
            return result;
        }
    }
}

/// Snapshot is pinned to the daemon's hello byte boundary, so later appends
/// cannot double-render events opened with that same `next_seq` cursor.
pub fn transcript(hello: &Value) -> io::Result<Value> {
    let Some(path) = hello["transcript_path"].as_str() else { return Ok(json!({"turns":[],"dropped_turns":0})); };
    let Some(size) = hello["transcript_bytes"].as_u64() else { return Ok(json!({"turns":[],"dropped_turns":0})); };
    if size == 0 { return Ok(json!({"turns":[],"dropped_turns":0})); }
    let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(Path::new(path))?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 || meta.len() < size {
        return Err(invalid("unsafe transcript snapshot"));
    }
    let start = size.saturating_sub(SNAPSHOT_BYTES);
    file.seek(SeekFrom::Start(start))?;
    let mut raw = vec![0; (size - start) as usize];
    file.read_exact(&mut raw)?;
    if start > 0 {
        let first = raw.iter().position(|byte| *byte == b'\n').ok_or_else(|| invalid("no complete transcript line"))?;
        raw.drain(..=first);
    }
    let mut turns: Vec<Value> = Vec::new();
    for line in raw.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        let content = &record["message"]["content"];
        match record["type"].as_str() {
            Some("user") if content.is_string() => turns.push(json!({"prompt":content,"text":"","tools":[]})),
            Some("assistant") if content.is_array() => {
                if turns.is_empty() { turns.push(json!({"prompt":"","text":"","tools":[]})); }
                let turn = turns.last_mut().expect("created above");
                for block in content.as_array().expect("checked above") {
                    match block["type"].as_str() {
                        Some("text") => if let Some(text) = block["text"].as_str() {
                            let current = turn["text"].as_str().unwrap_or("");
                            let room = MAX_TEXT.saturating_sub(current.chars().count());
                            if room > 0 { turn["text"] = json!(format!("{}{}", current, text.chars().take(room).collect::<String>())); }
                            if text.chars().count() > room { turn["text_truncated"] = json!(true); }
                        },
                        Some("tool_use") => if turn["tools"].as_array().is_some_and(|tools| tools.len() < 30) {
                            turn["tools"].as_array_mut().unwrap().push(json!({"call_id":block["id"],"name":block["name"],"result":null}));
                        },
                        _ => {}
                    }
                }
            },
            Some("user") if content.is_array() => {
                if let Some(turn) = turns.last_mut() {
                    for block in content.as_array().unwrap() {
                        if block["type"] == "tool_result" {
                            if let Some(tool) = turn["tools"].as_array_mut().and_then(|items|items.iter_mut().find(|tool| tool["call_id"] == block["tool_use_id"])) {
                                tool["result"] = block["content"].clone();
                            }
                        }
                    }
                }
            },
            _ => {}
        }
    }
    let dropped = turns.len().saturating_sub(MAX_TURNS);
    Ok(json!({"turns":turns.into_iter().skip(dropped).collect::<Vec<_>>(),"dropped_turns":dropped,
        "next_seq":hello.get("transcript_seq").unwrap_or(&hello["next_seq"])}))
}

pub fn validate_socket(path: &Path, runtime: &Path) -> io::Result<()> {
    use std::os::unix::fs::FileTypeExt;
    if path.parent() != Some(runtime) { return Err(invalid("daemon socket escaped runtime")); }
    let meta = std::fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err(invalid("unsafe daemon socket"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use doxa_runtime::{Daemon, Host, Session};
    use std::sync::Arc;
    struct Fixture;
    impl Host for Fixture {
        fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"turn_started","data":{"prompt":text}}));
            emit(json!({"type":"text_delta","data":{"text":"response"}}));
            emit(json!({"type":"turn_done","data":{}}));
        }
        fn call(&self, _: &str, _: &Value) -> Result<Value,String> { Ok(json!({})) }
    }
    #[test] fn native_daemon_wire_replays_and_accepts_prompt() {
        let dir = tempfile::tempdir().unwrap();
        let mut daemon = Daemon::bind(dir.path(),Session{session_id:"remote-fixture".into(),cwd:"/fixture".into(),model:None,engine:"fixture".into(),doxa_version:"test".into()},Arc::new(Fixture)).unwrap().start();
        let stream = UnixStream::connect(daemon.socket_path()).unwrap();
        let mut client = Client::from_stream(stream,"remote-fixture",Some("owner@example.com"),None).unwrap();
        let _ = client.call("status",json!({})).unwrap();
        assert_eq!(client.remote_prompt("hello",false).unwrap()["ok"],true);
        let mut types=Vec::new();
        for _ in 0..3 { types.push(client.next().unwrap()["event"]["type"].as_str().unwrap().to_owned()); }
        assert_eq!(types,vec!["turn_started","text_delta","turn_done"]);
        daemon.shutdown();
    }
    #[test] fn transcript_boundary_and_turn_cap() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session.jsonl");
        let mut bytes = Vec::new();
        for i in 0..42 { bytes.extend_from_slice(format!("{{\"type\":\"user\",\"message\":{{\"content\":\"p{i}\"}}}}\n").as_bytes()); }
        std::fs::write(&path, &bytes).unwrap();
        let result = transcript(&json!({"transcript_path":path,"transcript_bytes":bytes.len(),"next_seq":77})).unwrap();
        assert_eq!(result["dropped_turns"], 2);
        assert_eq!(result["turns"][0]["prompt"], "p2");
        assert_eq!(result["next_seq"], 77);
    }
}
