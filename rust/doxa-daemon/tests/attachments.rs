//! Real daemon-socket uploads, without clipboard reads or provider requests.
use doxa_runtime::{Daemon,Host,Session,PeerTurnOrigin};
use doxa_tui::transport::DaemonClient;
use doxa_input::Attachment;
use serde_json::{json,Value};
use std::{sync::{Arc,mpsc},time::Duration};
struct Capture(mpsc::SyncSender<(String,Vec<Attachment>)>);
impl Host for Capture {
    fn supports_images(&self)->bool{true}
    fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)){self.prompt_with_images(text,&[],None,emit)}
    fn prompt_with_images(&self,text:&str,images:&[Attachment],_:Option<&PeerTurnOrigin>,emit:&mut dyn FnMut(Value)){
        self.0.send((text.into(),images.to_vec())).unwrap();emit(json!({"type":"turn_started","data":{"prompt":text}}));emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self,_:&str,_:&Value)->Result<Value,String>{Err("unsupported".into())}
}
#[test]fn selected_content_crosses_bounded_frames_as_bytes_and_text_not_host_paths(){
    let root=tempfile::tempdir().unwrap();let(tx,rx)=mpsc::sync_channel(1);
    let session=Session{session_id:"attachment-fixture".into(),cwd:root.path().to_string_lossy().into_owned(),model:None,engine:"fixture".into(),doxa_version:"test".into()};
    let mut daemon=Daemon::bind(root.path(),session,Arc::new(Capture(tx))).unwrap().start();
    let mut client=DaemonClient::connect(daemon.socket_path(),None).unwrap();
    let png=doxa_input::decode_chunk("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8pcHFwMDAxMDAwMDAAAAPKgEwVo7a6wAAAABJRU5ErkJggg==").unwrap();
    let image=Attachment::from_source("fixture.png",png).unwrap();
    let text=Attachment::from_source("source.txt",vec![b'a';60*1024]).unwrap();
    let reply=client.prompt_attachments("inspect",&[image.clone(),text]).unwrap();assert_eq!(reply["ok"],true,"{reply}");
    let(prompt,images)=rx.recv_timeout(Duration::from_secs(5)).unwrap();assert!(prompt.contains("quoted source data"));assert!(prompt.contains(&"a".repeat(60*1024)));assert_eq!(images.len(),1);assert_eq!(images[0].bytes,image.bytes);
    assert!(!prompt.contains(&image.encoded()));drop(client);daemon.shutdown();
}

#[test]fn maximum_text_attachments_keep_live_prompt_cards_bounded_without_clipping_provider_input(){
    let root=tempfile::tempdir().unwrap();let(tx,rx)=mpsc::sync_channel(1);
    let session=Session{session_id:"large-attachment-fixture".into(),cwd:root.path().to_string_lossy().into_owned(),model:None,engine:"fixture".into(),doxa_version:"test".into()};
    let mut daemon=Daemon::bind(root.path(),session,Arc::new(Capture(tx))).unwrap().start();let mut client=DaemonClient::connect(daemon.socket_path(),None).unwrap();
    let files=(0..4).map(|index|Attachment::from_source(&format!("source-{index}.txt"),vec![b'a';doxa_input::MAX_TEXT]).unwrap()).collect::<Vec<_>>();
    let question="\"".repeat(30*1024);let reply=client.prompt_attachments(&question,&files).unwrap();assert_eq!(reply["ok"],true,"{reply}");
    let(prompt,images)=rx.recv_timeout(Duration::from_secs(5)).unwrap();assert!(images.is_empty());assert!(prompt.starts_with(&question));assert_eq!(prompt.matches(&"a".repeat(doxa_input::MAX_TEXT)).count(),4);
    let deadline=std::time::Instant::now()+Duration::from_secs(5);
    loop{assert!(std::time::Instant::now()<deadline,"live prompt missing");if let Some(frame)=client.poll_frame(Duration::from_millis(50)).unwrap(){if frame["event"]["type"]=="turn_started"{let data=&frame["event"]["data"];assert_ne!(data["truncated"],true);let shown=data["prompt"].as_str().unwrap();assert!(shown.contains("Prompt excerpt"));assert!(shown.contains("source-3.txt"));assert!(!shown.contains(&"a".repeat(1024)));assert!(serde_json::to_vec(&frame).unwrap().len()<doxa_runtime::MAX_FRAME_BYTES);break;}}}
    drop(client);daemon.shutdown();
}

struct ExpandingScrubber(Capture);
impl Host for ExpandingScrubber {
    fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)){self.0.prompt(text,emit)}
    fn public_prompt(&self,_:&str)->Result<String,String>{Ok("\u{0}".repeat(16*1024))}
    fn call(&self,method:&str,params:&Value)->Result<Value,String>{self.0.call(method,params)}
}
#[test]fn scrubber_expansion_cannot_overflow_attachment_event_frames(){
    let root=tempfile::tempdir().unwrap();let(tx,rx)=mpsc::sync_channel(1);let session=Session{session_id:"scrub-fixture".into(),cwd:root.path().to_string_lossy().into_owned(),model:None,engine:"fixture".into(),doxa_version:"test".into()};
    let mut daemon=Daemon::bind(root.path(),session,Arc::new(ExpandingScrubber(Capture(tx)))).unwrap().start();let mut client=DaemonClient::connect(daemon.socket_path(),None).unwrap();
    let file=Attachment::from_source("source.txt",b"source data".to_vec()).unwrap();assert_eq!(client.prompt_attachments("inspect",&[file]).unwrap()["ok"],true);assert!(rx.recv_timeout(Duration::from_secs(5)).unwrap().0.contains("source data"));
    let deadline=std::time::Instant::now()+Duration::from_secs(5);loop{assert!(std::time::Instant::now()<deadline);if let Some(frame)=client.poll_frame(Duration::from_millis(50)).unwrap(){if frame["event"]["type"]=="turn_started"{assert_ne!(frame["event"]["data"]["truncated"],true);assert!(frame["event"]["data"]["prompt"].as_str().unwrap().contains("Display excerpt"));assert!(serde_json::to_vec(&frame).unwrap().len()<doxa_runtime::MAX_FRAME_BYTES);break;}}}
    drop(client);daemon.shutdown();
}
