use doxa_protocol::{decode_line, encode_line, Direction, LineBuffer, WireError, MAX_FRAME_BYTES};
use serde_json::{json, Value};

#[test]
fn python_v1_frame_shapes_round_trip_with_extension_fields() {
    let server = Direction::ServerToClient;
    let client = Direction::ClientToServer;
    let frames = [
        (server, json!({"type":"hello","proto":1,"doxa":"1.19.0","session_id":"abc-1","model":null,"engine":"codex","cwd":"/tmp/project","next_seq":7,"permission_mode":"default","bypass_armed":false,"transcript_path":null})),
        (client, json!({"type":"attach","cursor":7,"remote_login":"alice"})),
        (client, json!({"type":"prompt","id":1,"text":"hello"})),
        (client, json!({"type":"call","id":2,"method":"status","params":{}})),
        (server, json!({"type":"event","seq":7,"turn":"aabbccddeeff","event":{"type":"text_delta","data":{"text":"hello"}},"future_field":true})),
        (server, json!({"type":"reply","id":2,"ok":true,"status":{"running":false,"queued":0}})),
    ];
    for (direction, frame) in frames {
        let bytes = encode_line(&frame, direction).unwrap();
        assert_eq!(decode_line(&bytes, direction).unwrap(), frame);
    }
}

#[test]
fn malformed_and_wrong_direction_frames_are_rejected() {
    let server = Direction::ServerToClient;
    let client = Direction::ClientToServer;
    assert_eq!(decode_line(b"[]\n", server), Err(WireError::InvalidJson));
    assert_eq!(decode_line(b"{}\n", server), Err(WireError::InvalidField("type")));
    assert_eq!(decode_line(b"{\"type\":\"hello\",\"proto\":2}\n", server), Err(WireError::UnsupportedVersion(2)));
    assert_eq!(decode_line(b"{\"type\":\"attach\",\"cursor\":-1}\n", client), Err(WireError::InvalidField("cursor")));
    assert_eq!(decode_line(b"{\"type\":\"reply\",\"id\":1,\"ok\":true}\n", client), Err(WireError::UnknownType));
    assert_eq!(decode_line(b"{\"type\":\"event\",\"seq\":0,\"event\":{\"type\":\"x\",\"data\":[]}}\n", server), Err(WireError::InvalidField("event.data")));
    assert_eq!(decode_line(b"{}", server), Err(WireError::IncompleteFrame));
    assert_eq!(decode_line(b"{not json}\n", server), Err(WireError::InvalidJson));
}

#[test]
fn frame_size_and_chunk_boundaries_are_enforced() {
    let frame = json!({"type":"prompt","id":1,"text":"x".repeat(MAX_FRAME_BYTES)});
    assert_eq!(encode_line(&frame, Direction::ClientToServer), Err(WireError::FrameTooLarge));
    let mut buffer = LineBuffer::default();
    assert!(buffer.push(b"{\"type\":\"attach\",").unwrap().is_empty());
    assert_eq!(buffer.finish(), Err(WireError::IncompleteFrame));
    let lines = buffer.push(b"\"cursor\":null}\n{\"type\":\"attach\",\"cursor\":0}\n").unwrap();
    assert_eq!(lines.len(), 2);
    for line in lines { decode_line(&line, Direction::ClientToServer).unwrap(); }
    assert_eq!(buffer.finish(), Ok(()));
    assert_eq!(buffer.push(&vec![b'x'; MAX_FRAME_BYTES + 1]), Err(WireError::FrameTooLarge));
    assert_eq!(buffer.finish(), Ok(()));
}

#[test]
fn cap_includes_the_newline_byte() {
    let base = json!({"type":"prompt","id":0,"text":""});
    let overhead = encode_line(&base, Direction::ClientToServer).unwrap().len();
    let exact: Value = json!({"type":"prompt","id":0,"text":"x".repeat(MAX_FRAME_BYTES - overhead)});
    assert_eq!(encode_line(&exact, Direction::ClientToServer).unwrap().len(), MAX_FRAME_BYTES);
    let too_long: Value = json!({"type":"prompt","id":0,"text":"x".repeat(MAX_FRAME_BYTES - overhead + 1)});
    assert_eq!(encode_line(&too_long, Direction::ClientToServer), Err(WireError::FrameTooLarge));
}
