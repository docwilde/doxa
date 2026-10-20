//! Contract from openai/codex b412ff32c417f855c2b2d1581b77058eed87c84b:
//! app-server-protocol/src/protocol/v2/turn.rs, ImageReference + UserInput.
//! This is the app-server schema, distinct from the Responses/core schema.
use serde::Deserialize;
use serde_json::json;
#[derive(Deserialize)]
#[serde(untagged)]
enum ImageReference { Inline { url:String }, File { #[serde(rename="fileId")] file_id:String } }
#[derive(Deserialize)]
struct ImageInput { #[serde(rename="type")] kind:String, #[serde(flatten)] image:ImageReference }
#[test]
fn codex_appserver_contract_uses_url_not_responses_image_url(){
    let bytes=doxa_input::decode_chunk("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGMU0QhgYGBgYmBgYGBgAAAGfgCQXwjczwAAAABJRU5ErkJggg==").unwrap();
    let image=doxa_input::Attachment::from_source("fixture.png",bytes).unwrap();let block=image.codex_block();
    let decoded:ImageInput=serde_json::from_value(block.clone()).unwrap();assert_eq!(decoded.kind,"image");
    match decoded.image{ImageReference::Inline{url}=>assert!(url.starts_with("data:image/png;base64,")),ImageReference::File{file_id}=>panic!("unexpected file reference: {file_id}")}
    assert!(serde_json::from_value::<ImageInput>(json!({"type":"image","image_url":block["url"]})).is_err());
    println!("DOXA_CODEX_IMAGE_INPUT={}",json!({"threadId":"schema-fixture-no-session-created","input":[{"type":"text","text":"synthetic schema fixture","text_elements":[]},block]}));
}
