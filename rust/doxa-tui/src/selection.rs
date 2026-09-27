//! Selection of visible terminal cells; provider source/hidden rows never enter it.
use ratatui::{buffer::Buffer, layout::{Position, Rect}, style::Modifier};
use unicode_width::UnicodeWidthStr;
const CELL_CAP: usize = 64 * 1024;
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Owner { pub pane: usize, pub session: String }
#[derive(Clone, Debug, PartialEq, Eq)]
struct Surface { owner: Owner, rect: Rect, cells: Vec<Option<String>> }
#[derive(Clone, Debug)]
struct Range { surface: Surface, anchor: Position, end: Position, dragged: bool }
#[derive(Debug, Default)]
pub struct Selection { surfaces: Vec<Surface>, regions:Vec<(Owner,Rect)>, range: Option<Range> }
impl Selection {
    pub fn begin_frame(&mut self) { self.surfaces.clear(); self.regions.clear(); }
    pub fn register(&mut self,owner:Owner,rect:Rect) {self.regions.push((owner,rect));}
    pub fn finish_paint(&mut self,buffer:&mut Buffer) {
        for(owner,rect)in std::mem::take(&mut self.regions){self.capture(owner,rect,buffer);}
        self.finish_frame();
    }
    pub fn capture(&mut self, owner: Owner, rect: Rect, buffer: &mut Buffer) {
        let count = usize::from(rect.width) * usize::from(rect.height);
        if count == 0 || count > CELL_CAP || self.surfaces.iter().map(|s|s.cells.len()).sum::<usize>() + count > CELL_CAP { return; }
        let mut cells = Vec::with_capacity(count);
        for y in rect.y..rect.bottom() {
            let mut continued = 0;
            for x in rect.x..rect.right() {
                if continued > 0 { cells.push(None); continued -= 1; }
                else { let symbol = buffer[(x,y)].symbol().to_owned(); continued = symbol.width().saturating_sub(1); cells.push(Some(symbol)); }
            }
        }
        let surface = Surface { owner, rect, cells };
        if let Some(range) = self.range.as_ref().filter(|r|r.surface.owner == surface.owner) {
            if range.surface != surface { self.range = None; }
            else if range.dragged {
                let (first,last) = endpoints(range);
                for y in rect.y..rect.bottom() { for x in rect.x..rect.right() {
                    let offset=usize::from(y-rect.y)*usize::from(rect.width)+usize::from(x-rect.x);
                    if let Some(symbol)=&surface.cells[offset] {
                        let end=x.saturating_add(symbol.width().max(1) as u16).saturating_sub(1).min(rect.right()-1);
                        if (y,end)>=first && (y,x)<=last { for cell in x..=end {buffer[(cell,y)].modifier |= Modifier::REVERSED;} }
                    }
                }}
            }
        }
        self.surfaces.push(surface);
    }
    pub fn finish_frame(&mut self) {
        if self.range.as_ref().is_some_and(|range| !self.surfaces.iter().any(|surface|surface.owner == range.surface.owner)) { self.range = None; }
    }
    pub fn start(&mut self, point: Position) -> Option<Owner> {
        self.range = None;
        let surface = self.surfaces.iter().find(|s|s.rect.contains(point))?.clone();
        let owner = surface.owner.clone();
        self.range = Some(Range { surface, anchor: point, end: point, dragged: false }); Some(owner)
    }
    pub fn drag(&mut self, point: Position) -> bool {
        let Some(range) = &mut self.range else { return false; };
        let rect = range.surface.rect;
        range.end = Position::new(point.x.clamp(rect.x,rect.right()-1),point.y.clamp(rect.y,rect.bottom()-1));
        range.dragged |= range.end != range.anchor; true
    }
    pub fn clear(&mut self) -> bool { self.range.take().is_some() }
    pub fn belongs_to(&self,owner:&Owner)->bool {self.range.as_ref().is_some_and(|range|&range.surface.owner==owner)}
    pub fn text(&self, owner: &Owner) -> Option<String> {
        let range = self.range.as_ref().filter(|r|r.dragged && &r.surface.owner == owner)?;
        let (first,last) = endpoints(range); let rect = range.surface.rect; let mut lines = Vec::new();
        for y in first.0..=last.0 {
            let start = if y==first.0 { first.1 } else { rect.x };
            let end = if y==last.0 { last.1 } else { rect.right()-1 };
            let mut line = String::new();
            for x in rect.x..rect.right() {
                if let Some(symbol) = &range.surface.cells[usize::from(y-rect.y)*usize::from(rect.width)+usize::from(x-rect.x)] {
                    let glyph_end=x.saturating_add(symbol.width().max(1) as u16).saturating_sub(1);
                    if glyph_end>=start && x<=end { line.push_str(symbol); }
                }
            }
            lines.push(line.trim_end().to_owned());
        }
        Some(lines.join("\n"))
    }
}
fn endpoints(range:&Range)->((u16,u16),(u16,u16)) {
    let anchor=(range.anchor.y,range.anchor.x);let end=(range.end.y,range.end.x); (anchor.min(end),anchor.max(end))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn final_paint_snapshot_includes_overlay_text_and_geometry_changes_clear_selection() {
        let owner=Owner{pane:0,session:"s".into()};let rect=Rect::new(1,1,8,1);let mut buffer=Buffer::empty(Rect::new(0,0,20,8));
        let mut state=Selection::default();state.begin_frame();state.register(owner.clone(),rect);
        buffer.set_string(1,1,"tooltip",ratatui::style::Style::default());state.finish_paint(&mut buffer);
        state.start(Position::new(1,1));state.drag(Position::new(7,1));assert_eq!(state.text(&owner).as_deref(),Some("tooltip"));
        state.begin_frame();state.register(owner.clone(),Rect::new(2,1,7,1));state.finish_paint(&mut buffer);assert!(state.text(&owner).is_none());
    }
    #[test]
    fn selection_uses_painted_wide_cells_and_line_breaks_and_invalidates_changed_surface() {
        let owner=Owner {pane:1,session:"s".into()};let rect=Rect::new(4,2,8,2);let mut buffer=Buffer::empty(Rect::new(0,0,20,8));
        buffer.set_string(4,2,"a界bc",ratatui::style::Style::default());buffer.set_string(4,3,"next",ratatui::style::Style::default());
        let mut state=Selection::default();state.capture(owner.clone(),rect,&mut buffer);
        state.start(Position::new(6,2));state.drag(Position::new(6,3));assert_eq!(state.text(&owner).as_deref(),Some("界bc\nnex"));
        assert!(state.text(&Owner{pane:0,session:"s".into()}).is_none());
        state.begin_frame();state.capture(owner.clone(),rect,&mut buffer);assert!(buffer[(6,2)].modifier.contains(Modifier::REVERSED));
        buffer.set_string(4,2,"changed",ratatui::style::Style::default());state.begin_frame();state.capture(owner.clone(),rect,&mut buffer);assert!(state.text(&owner).is_none());
    }
}
