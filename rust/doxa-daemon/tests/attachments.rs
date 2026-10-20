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
        self.0.send((text.into(),images.to_vec())).unwrap();emit(json!({"type":"turn_done","data":{}}));
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
