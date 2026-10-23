use doxa_peers::delivery::{self,Inbox,Ledger,Message,Sender,TurnRef,PeerFrame};
use serde_json::json;
use std::os::unix::fs::PermissionsExt;
fn wire(body:&str)->String{format!("{}{}",doxa_protocol::peer::FLEET_PREFIX,json!({"v":1,"fleet_id":"fleet","message_id":"message","from_session":"sender","to_session":"receiver","kind":"status","assignment_id":"assignment","charter_sha256":"a".repeat(64),"in_reply_to":null,"body":body,"artifact_refs":[],"requested_action":null,"hop":0}))}

#[test]
fn maximum_fleet_body_crosses_real_kernel_authenticated_socket_without_truncation(){
    let dir=tempfile::tempdir().unwrap();std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();
    let inbox=Inbox::bind(dir.path(),"receiver").unwrap();
    for body in ["x".repeat(16384),"🧪".repeat(4096),"\"".repeat(16384),"\u{1}".repeat(16384)]{
        let wire=wire(&body);let frame=PeerFrame{authenticated_pid:None,from_id:"sender".into(),from_title:"Sender".into(),sent_at:"now".into(),body:wire.clone(),from_repo:Some("/repo".into()),kind:Some("direct".into())};
        let path=inbox.path().to_owned();let sender=std::thread::spawn(move||delivery::send(&path,&frame));let received=inbox.receive(&|text:&str|text.to_owned()).unwrap();sender.join().unwrap().unwrap();assert_eq!(received.body,wire);assert_eq!(received.authenticated_pid,Some(std::process::id() as i32));let parsed:serde_json::Value=serde_json::from_str(received.body.strip_prefix(doxa_protocol::peer::FLEET_PREFIX).unwrap()).unwrap();assert_eq!(parsed["body"],body);
    }
    let invalid=PeerFrame{authenticated_pid:None,from_id:"sender".into(),from_title:"Sender".into(),sent_at:"now".into(),body:wire(&"x".repeat(16385)),from_repo:None,kind:Some("fleet".into())};
    assert!(delivery::send(inbox.path(),&invalid).is_err());assert!(inbox.poll_receive(&|text:&str|text.to_owned()).unwrap().is_none());
    assert!(delivery::body_within_limit(&"🧪".repeat(8000)));assert!(!delivery::body_within_limit(&"x".repeat(8001)));
}

#[test]
fn latest_three_maximum_escaped_fleet_reports_remain_complete_in_durable_history(){
    let dir=tempfile::tempdir().unwrap();std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();let ledger=Ledger::new(dir.path().join("messages.jsonl"));
    let body="\u{1}".repeat(16384);let encoded=wire(&body);
    for index in 0..3{ledger.append(Message{v:1,id:format!("message-{index}"),ts:"now".into(),sender:Sender{session:"sender".into(),title:None,repo:Some("/repo".into()),model:None,engine:None},to:vec!["receiver".into()],kind:"direct".into(),in_reply_to:None,body:encoded.clone(),body_sha256:String::new(),latency_ms:None,turn:TurnRef{id:None,state:"completed".into()}},&|text:&str|text.to_owned()).unwrap();}
    let messages=ledger.history_filtered("receiver","/repo","received",3,&|text:&str|text.to_owned()).unwrap();assert_eq!(messages.len(),3);assert!(serde_json::to_vec(&messages).unwrap().len()<=doxa_protocol::peer::MAX_HISTORY_BYTES);for message in &messages{assert_eq!(message.body,encoded);}
    assert_eq!(ledger.history("receiver","/repo",&|text:&str|text.to_owned()).unwrap().len(),3);
}
