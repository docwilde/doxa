//! Bounded, explicitly selected prompt content. Never fetch a URL or follow a
//! provider-supplied host path. Image bytes travel through the provider pipe.
use base64::{engine::general_purpose::STANDARD, Engine};
use image::{GenericImageView, ImageReader, ImageFormat};
use serde_json::{json, Value};
use std::{fs::OpenOptions, io::{self, Cursor, Read}, os::unix::fs::OpenOptionsExt,
    path::Path, sync::Arc};

pub const MAX_COUNT: usize = 4;
pub const MAX_TOTAL: usize = 4 * 1024 * 1024;
pub const MAX_IMAGE: usize = 1024 * 1024;
pub const MAX_SOURCE: usize = 8 * 1024 * 1024;
pub const MAX_TEXT: usize = 64 * 1024;
pub const CHUNK: usize = 24 * 1024;
const MAX_PIXELS: u64 = 32_000_000;

#[derive(Clone)]
pub struct Attachment { pub name: String, pub mime: String, pub bytes: Arc<Vec<u8>> }
impl std::fmt::Debug for Attachment {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result{f.debug_struct("Attachment").field("name",&self.name).field("mime",&self.mime).field("bytes",&self.bytes.len()).finish()}
}
fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }
pub fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && !name.chars().any(char::is_control)
        && !name.contains(['/', '\\'])
}
impl Attachment {
    pub fn load(path: &Path) -> io::Result<Self> {
        let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > MAX_SOURCE as u64 { return Err(invalid("Choose a regular file of at most 8 MiB")); }
        let name = path.file_name().and_then(|s|s.to_str()).filter(|s|valid_name(s))
            .ok_or_else(||invalid("Attachment filename is invalid or longer than 128 bytes"))?;
        let mut raw = Vec::new(); file.by_ref().take((MAX_SOURCE + 1) as u64).read_to_end(&mut raw)?;
        Self::from_source(name, raw)
    }
    pub fn from_source(name: &str, raw: Vec<u8>) -> io::Result<Self> {
        if !valid_name(name) || raw.is_empty() || raw.len() > MAX_SOURCE { return Err(invalid("Attachment is empty, invalid, or exceeds 8 MiB")); }
        if let Ok(format) = image::guess_format(&raw) {
            if !matches!(format,ImageFormat::Png|ImageFormat::Jpeg|ImageFormat::Gif|ImageFormat::WebP) {return Err(invalid("Use PNG, JPEG, GIF, or WebP images"));}
            let mut reader = ImageReader::with_format(Cursor::new(&raw), format);
            let mut limits=image::Limits::default(); limits.max_alloc=Some(128*1024*1024); reader.limits(limits);
            let (width,height)=ImageReader::with_format(Cursor::new(&raw),format).into_dimensions().map_err(|_|invalid("Invalid image header"))?;
            if width==0||height==0||u64::from(width)*u64::from(height)>MAX_PIXELS {return Err(invalid("Image exceeds 32 million pixels"));}
            let decoded=reader.decode().map_err(|_|invalid("Image could not be decoded"))?;
            // Canonical encoding strips metadata and freezes the first frame.
            let mut image=if width>2048||height>2048 {decoded.resize(2048,2048,image::imageops::FilterType::Lanczos3)}else{decoded};
            let mut png=Cursor::new(Vec::new()); image.write_to(&mut png,ImageFormat::Png).map_err(|_|invalid("Image encoding failed"))?;
            let (mime,bytes)=if png.get_ref().len()<=MAX_IMAGE {("image/png",png.into_inner())} else {
                // Composite transparent pixels over white before JPEG encoding.
                let mut rgba=image.to_rgba8();for p in rgba.pixels_mut(){let a=u16::from(p[3]);for c in 0..3{p[c]=((u16::from(p[c])*a+255*(255-a))/255)as u8;}p[3]=255;}
                image=image::DynamicImage::ImageRgba8(rgba);
                let mut bytes=Vec::new();
                loop {bytes.clear();image::codecs::jpeg::JpegEncoder::new_with_quality(&mut bytes,85).encode_image(&image.to_rgb8()).map_err(|_|invalid("Image encoding failed"))?;
                    if bytes.len()<=MAX_IMAGE {break;} let(w,h)=image.dimensions();if w<=256||h<=256{return Err(invalid("Image exceeds attachment limit"));}image=image.resize((w*3/4).max(1),(h*3/4).max(1),image::imageops::FilterType::Triangle);}
                ("image/jpeg",bytes)
            };
            return Ok(Self{name:name.into(),mime:mime.into(),bytes:Arc::new(bytes)});
        }
        if raw.starts_with(b"%PDF-"){return Err(invalid("PDF attachment input is not supported yet"));}
        let text=std::str::from_utf8(&raw).map_err(|_|invalid("Use an image or UTF-8 text file; other binary files are unsupported"))?;
        if raw.len()>MAX_TEXT||text.chars().any(|c|c.is_control()&&!matches!(c,'\n'|'\r'|'\t')) {return Err(invalid("Text attachment must be UTF-8, without binary controls, and at most 64 KiB"));}
        Ok(Self{name:name.into(),mime:"text/plain".into(),bytes:Arc::new(raw)})
    }
    /// Validate untrusted uploaded bytes without silently changing their type.
    pub fn from_wire(name: &str, mime: &str, bytes: Vec<u8>) -> io::Result<Self> {
        if bytes.len()>MAX_IMAGE {return Err(invalid("Attachment exceeds 1 MiB"));}
        if !valid_name(name)||bytes.is_empty(){return Err(invalid("Invalid attachment"));}
        if mime=="text/plain" {return Self::from_source(name,bytes).and_then(|v|if v.mime==mime {Ok(v)} else {Err(invalid("Attachment media type mismatch"))});}
        let expected=match mime {"image/png"=>ImageFormat::Png,"image/jpeg"=>ImageFormat::Jpeg,_=>return Err(invalid("Unsupported image media type"))};
        if image::guess_format(&bytes).ok()!=Some(expected){return Err(invalid("Attachment media type does not match its content"));}
        let (w,h)=ImageReader::with_format(Cursor::new(&bytes),expected).into_dimensions().map_err(|_|invalid("Invalid image"))?;
        if w==0||h==0||u64::from(w)*u64::from(h)>MAX_PIXELS{return Err(invalid("Image exceeds 32 million pixels"));}
        let mut reader=ImageReader::with_format(Cursor::new(&bytes),expected);let mut limits=image::Limits::default();limits.max_alloc=Some(128*1024*1024);reader.limits(limits);
        reader.decode().map_err(|_|invalid("Invalid image"))?;
        Ok(Self{name:name.into(),mime:mime.into(),bytes:Arc::new(bytes)})
    }
    pub fn is_image(&self)->bool {self.mime.starts_with("image/")}
    pub fn encoded(&self)->String {STANDARD.encode(&*self.bytes)}
    pub fn claude_block(&self)->Value {json!({"type":"image","source":{"type":"base64","media_type":self.mime,"data":self.encoded()}})}
    // App-server UserInput::Image uses `url`; Responses/core uses `image_url`.
    pub fn codex_block(&self)->Value {json!({"type":"image","url":format!("data:{};base64,{}",self.mime,self.encoded())})}
}
pub fn decode_chunk(value:&str)->io::Result<Vec<u8>> {
    if value.len()>CHUNK*4/3+4 {return Err(invalid("Attachment chunk too large"));}
    let bytes=STANDARD.decode(value).map_err(|_|invalid("Invalid attachment encoding"))?;
    if bytes.len()>CHUNK {return Err(invalid("Attachment chunk too large"));}Ok(bytes)
}
pub fn encode_chunk(value:&[u8])->String {STANDARD.encode(value)}
pub fn validate_set(values:&[Attachment])->io::Result<()> {
    if values.len()>MAX_COUNT||values.iter().map(|a|a.bytes.len()).sum::<usize>()>MAX_TOTAL{return Err(invalid("At most 4 attachments and 4 MiB per prompt"));}Ok(())
}
pub fn prompt_text(text:&str,values:&[Attachment])->String {
    let mut result=text.to_owned();
    for value in values {
        if value.is_image(){result.push_str(&format!("\n\n[Attached image: {}]",value.name));}
        else {result.push_str(&format!("\n\n--- attached UTF-8 file {} (quoted source data) ---\n{}\n--- end attached file ---",value.name,String::from_utf8_lossy(&value.bytes)));}
    }result
}
pub fn display_text(text:&str,values:&[Attachment])->String {
    // At most 48 KiB even for JSON-escaped controls, leaving room in the v1
    // frame for labels and provenance. Provider input is never clipped here.
    let mut end=text.len().min(8*1024);while !text.is_char_boundary(end){end-=1;}
    let mut result=text[..end].to_owned();
    if end<text.len(){result.push_str("\n[Prompt excerpt; full input retained in transcript]");}
    for value in values {result.push_str(&format!("\n[Attachment: {} · {} bytes]",value.name,value.bytes.len()));}result
}

#[cfg(test)]mod tests {
    use super::*;
    #[test]fn selected_files_are_bounded_and_do_not_follow_links(){let dir=tempfile::tempdir().unwrap();let p=dir.path().join("notes.txt");std::fs::write(&p,"hello").unwrap();assert!(!Attachment::load(&p).unwrap().is_image());let link=dir.path().join("link");std::os::unix::fs::symlink(&p,&link).unwrap();assert!(Attachment::load(&link).is_err());assert!(Attachment::from_source("x.pdf",b"%PDF-1\x00".to_vec()).is_err());assert!(Attachment::from_source("x",vec![b'x';MAX_TEXT+1]).is_err());}
    #[test]fn image_bytes_use_real_provider_blocks_and_drop_metadata(){let img=image::DynamicImage::new_rgb8(3,2);let mut png=Cursor::new(Vec::new());img.write_to(&mut png,ImageFormat::Png).unwrap();let a=Attachment::from_source("screenshot.png",png.into_inner()).unwrap();assert!(a.is_image());assert_eq!(a.claude_block()["source"]["media_type"],"image/png");assert!(a.codex_block()["url"].as_str().unwrap().starts_with("data:image/png;base64,"));assert!(Attachment::from_wire("x","image/jpeg",(*a.bytes).clone()).is_err());assert!(!display_text("look",&[a.clone()]).contains(&a.encoded()));assert!(prompt_text("look",&[a]).contains("Attached image"));}
    #[test]fn uploaded_chunks_and_aggregate_have_separate_limits(){assert!(decode_chunk(&encode_chunk(&vec![1;CHUNK+1])).is_err());assert!(decode_chunk("!?").is_err());let a=Attachment::from_source("x.txt",b"source".to_vec()).unwrap();assert!(validate_set(&vec![a;5]).is_err());}
}
