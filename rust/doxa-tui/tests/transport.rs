use doxa_tui::transport::{DaemonClient, TransportError, MAX_FRAME_BYTES};
use serde_json::{json, Map};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Duration;

static NEXT_SOCKET: AtomicUsize = AtomicUsize::new(0);

fn socket(test: impl FnOnce(UnixStream) + Send + 'static) -> (PathBuf, thread::JoinHandle<()>) {
    let path = std::env::temp_dir().join(format!(
        "doxa-rust-transport-{}-{}.sock", std::process::id(), NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
    ));
    let listener = UnixListener::bind(&path).unwrap();
    let task = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        test(stream);
    });
    (path, task)
}

fn send(stream: &mut UnixStream, value: serde_json::Value) {
    writeln!(stream, "{value}").unwrap();
}

fn hello(stream: &mut UnixStream) {
    send(stream, json!({"type":"hello","proto":1,"session_id":"session-1", "cwd":"/tmp", "model":null,"engine":"doxa","next_seq":7}));
}

fn line(reader: &mut BufReader<UnixStream>) -> serde_json::Value {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

fn finish(path: PathBuf, task: thread::JoinHandle<()>) {
    task.join().unwrap();
    std::fs::remove_file(path).unwrap();
}

#[test]
fn handshake_attach_replay_and_eof() {
    let (path, task) = socket(|mut stream| {
        hello(&mut stream);
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        assert_eq!(line(&mut reader), json!({"type":"attach","cursor":5}));
        send(&mut stream, json!({"type":"event","seq":5,"turn":null,"event":{"type":"text","data":{"text":"old"}}}));
        send(&mut stream, json!({"type":"event","seq":7,"turn":"turn-1","event":{"type":"turn_done","data":{}}}));
    });
    let mut client = DaemonClient::connect(&path, Some(5)).unwrap();
    assert_eq!(client.hello["session_id"], "session-1");
    assert_eq!(client.next_frame().unwrap()["seq"], 5);
    assert_eq!(client.cursor, 6);
    assert_eq!(client.next_frame().unwrap()["seq"], 7);
    assert_eq!(client.cursor, 8);
    assert!(matches!(client.next_frame(), Err(TransportError::Closed)));
    finish(path, task);
}

#[test]
fn prompt_and_call_correlate_replies_and_keep_intervening_events() {
    let (path, task) = socket(|mut stream| {
        hello(&mut stream);
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        assert_eq!(line(&mut reader), json!({"type":"attach","cursor":null}));
        let prompt = line(&mut reader);
        assert_eq!(prompt, json!({"type":"prompt","id":1,"text":"hi"}));
        send(&mut stream, json!({"type":"event","seq":7,"turn":null,"event":{"type":"peer_joined","data":{}}}));
        send(&mut stream, json!({"type":"reply","id":99,"ok":true}));
        send(&mut stream, json!({"type":"reply","id":1,"ok":true,"turn":"t1"}));
        let call = line(&mut reader);
        assert_eq!(call, json!({"type":"call","id":2,"method":"status","params":{}}));
        send(&mut stream, json!({"type":"reply","id":2,"ok":true,"model":"x"}));
    });
    let mut client = DaemonClient::connect(&path, None).unwrap();
    assert_eq!(client.prompt("hi").unwrap()["turn"], "t1");
    assert_eq!(client.call("status", Map::new()).unwrap()["model"], "x");
    assert_eq!(client.next_frame().unwrap()["event"]["type"], "peer_joined");
    assert_eq!(client.next_frame().unwrap()["id"], 99);
    finish(path, task);
}

#[test]
fn invalid_hello_and_frame_bounds_are_rejected() {
    for invalid in [
        json!({"type":"hello","proto":2,"session_id":"s","cwd":"/tmp","next_seq":0}),
        json!({"type":"hello","proto":1,"session_id":"","cwd":"/tmp","next_seq":0}),
        json!({"type":"hello","proto":1,"session_id":"s","cwd":"/tmp","next_seq":-1}),
    ] {
        let (path, task) = socket(move |mut stream| send(&mut stream, invalid));
        assert!(DaemonClient::connect(&path, None).is_err());
        finish(path, task);
    }
    let (path, task) = socket(|mut stream| {
        hello(&mut stream);
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        line(&mut reader);
        stream.write_all(&vec![b'x'; MAX_FRAME_BYTES + 1]).unwrap();
    });
    let mut client = DaemonClient::connect(&path, None).unwrap();
    assert!(matches!(client.next_frame(), Err(TransportError::FrameTooLarge)));
    finish(path, task);
}

#[test]
fn malformed_and_partial_frames_are_rejected() {
    for bytes in [b"garbage\n".to_vec(), b"{\"type\":\"event\"}\n".to_vec(), b"{\"type\":\"reply\"}".to_vec()] {
        let (path, task) = socket(move |mut stream| {
            hello(&mut stream);
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            line(&mut reader);
            stream.write_all(&bytes).unwrap();
        });
        let mut client = DaemonClient::connect(&path, None).unwrap();
        assert!(matches!(client.next_frame(), Err(TransportError::Malformed(_))));
        finish(path, task);
    }
}

#[test]
fn oversize_outbound_prompt_is_rejected_before_write() {
    let (path, task) = socket(|mut stream| {
        hello(&mut stream);
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        line(&mut reader);
    });
    let mut client = DaemonClient::connect(&path, None).unwrap();
    assert!(matches!(client.prompt(&"x".repeat(MAX_FRAME_BYTES)), Err(TransportError::FrameTooLarge)));
    finish(path, task);
}

#[test]
fn poll_preserves_partial_line_across_timeout() {
    let (path, task) = socket(|mut stream| {
        hello(&mut stream);
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        line(&mut reader);
        stream.write_all(b"{\"type\":\"event\",\"seq\":7,").unwrap();
        thread::sleep(Duration::from_millis(100));
        stream.write_all(b"\"turn\":null,\"event\":{\"type\":\"text\",\"data\":{}}}\n").unwrap();
    });
    let mut client = DaemonClient::connect(&path, None).unwrap();
    assert!(client.poll_frame(Duration::from_millis(20)).unwrap().is_none());
    assert_eq!(client.poll_frame(Duration::from_secs(1)).unwrap().unwrap()["seq"], 7);
    finish(path, task);
}
