//! Shared cell-width-safe rows for LORE lists; exact review content uses its
//! separate full-content renderer and is never taken from a clipped cell.
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub fn cell(text: &str, width: usize) -> String {
    let text=crate::markdown::sanitize(text).replace(['\n','\r','\t']," ");
    if width==0 {return String::new();}
    let truncated=text.width()>width;
    let budget=width.saturating_sub(usize::from(truncated));
    let mut result=String::new();let mut used=0;
    for ch in text.chars() {
        let n=ch.width().unwrap_or(0);
        if used+n>budget {break;}
        result.push(ch);used+=n;
    }
    if truncated {result.push('…');used+=1;}
    result.push_str(&" ".repeat(width.saturating_sub(used)));
    result
}

pub struct BeliefColumns { widths:Vec<usize>, pub actions:bool }
impl BeliefColumns {
    pub fn new(width:usize)->Self {
        let widths=if width>=105 {vec![17,7,16,5,4,19,width-74]}
            else if width>=72 {vec![17,6,12,5,4,0,width-50]}
            else if width>=42 {vec![17,5,8,0,0,0,width-33]}
            else {vec![0,5,0,0,0,0,width.saturating_sub(6)]};
        Self {actions:widths[0]>0,widths}
    }
    fn row(&self, fields:[&str;7])->String {
        fields.into_iter().zip(&self.widths).filter(|(_,width)|**width>0)
            .map(|(field,width)|cell(field,*width)).collect::<Vec<_>>().join(" ")
    }
    pub fn header(&self)->String {self.row(["Actions","ID","Subject","Conf","Ev","Updated","Claim"])}
    pub fn belief(&self,id:u64,subject:&str,claim:&str,confidence:f64,evidence:Option<u64>,updated:Option<&str>)->String {
        self.row(["[Accept] [Reject]",&format!("#{id}"),subject,&format!("{:.0}%",confidence*100.),
            &evidence.map(|n|n.to_string()).unwrap_or_else(||"—".into()),updated.unwrap_or("—"),claim])
    }
}

pub fn memory_header(width:usize)->String {memory_row("Scope","Fact","Source",width)}
pub fn memory_row(scope:&str,fact:&str,source:&str,width:usize)->String {
    if width<38 {return format!("{} {}",cell(scope,7),cell(fact,width.saturating_sub(8)));}
    let source_width=(width/4).min(28);let fact_width=width.saturating_sub(9+source_width);
    format!("{} {} {}",cell(scope,7),cell(fact,fact_width),cell(source,source_width))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn columns_remain_aligned_for_wide_unicode_and_terminal_controls() {
        for width in [24,42,72,105,140] {
            let columns=BeliefColumns::new(width);
            assert!(columns.header().width()<=width);
            assert!(columns.belief(7,"界界subject","claim\nwith \u{1b} control",0.9,Some(5),Some("2026-09-27T12:00:00Z")).width()<=width);
            assert!(memory_header(width).width()<=width);
            assert!(memory_row("project","界界very long single fact","actual source",width).width()<=width);
        }
        assert_eq!(cell("界界",3),"界…");
        assert_eq!(cell("a\nb",5),"a b  ");
    }
}
