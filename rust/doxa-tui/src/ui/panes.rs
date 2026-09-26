//! Bounded recursive group geometry. Session/tab records remain authoritative
//! in UiStateStore; this tree only names group indices and proportions.
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use super::Split;

pub const MAX_PANES: usize = 16;
pub const MAX_DEPTH: usize = 8;
pub const MAX_TABS: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Divider { path: [u8; MAX_DEPTH], depth: usize, index: usize }

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Tree {
    Group(usize),
    Split { orientation: Split, children: Vec<Tree>, weights: Vec<u16> },
}
impl Tree {
    pub fn pair(orientation: Split, percent: u16) -> Self {
        Self::Split { orientation, children: vec![Self::Group(0), Self::Group(1)],
            weights: vec![percent, 100 - percent] }
    }
    pub fn rects(&self, body: Rect, groups: usize) -> Vec<Rect> {
        let mut out = vec![Rect::default(); groups];
        self.walk(body, &mut out);
        out
    }
    fn walk(&self, body: Rect, out: &mut [Rect]) {
        match self {
            Self::Group(index) => { if let Some(rect) = out.get_mut(*index) { *rect = body; } }
            Self::Split { orientation, children, weights } => {
                let total = weights.iter().map(|w| u32::from(*w)).sum::<u32>().max(1);
                let constraints: Vec<_> = weights.iter().map(|w| Constraint::Ratio(u32::from(*w), total)).collect();
                let regions = Layout::default().direction(if *orientation == Split::Vertical { Direction::Horizontal } else { Direction::Vertical })
                    .constraints(constraints).split(body);
                for (child, rect) in children.iter().zip(regions.iter()) { child.walk(*rect, out); }
            }
        }
    }
    fn regions(body: Rect, orientation: Split, weights: &[u16]) -> Vec<Rect> {
        let total=weights.iter().map(|w|u32::from(*w)).sum::<u32>().max(1);
        Layout::default().direction(if orientation==Split::Vertical{Direction::Horizontal}else{Direction::Vertical})
            .constraints(weights.iter().map(|w|Constraint::Ratio(u32::from(*w),total)).collect::<Vec<_>>()).split(body).to_vec()
    }
    pub fn divider_at(&self,body:Rect,column:u16,row:u16)->Option<Divider>{
        fn hit(tree:&Tree,body:Rect,column:u16,row:u16,path:[u8;MAX_DEPTH],depth:usize)->Option<Divider>{
            let Tree::Split{orientation,children,weights}=tree else{return None;};
            let regions=Tree::regions(body,*orientation,weights);
            if depth<MAX_DEPTH {
                for(index,(child,rect))in children.iter().zip(&regions).enumerate(){
                    let mut path=path;path[depth]=index as u8;
                    if let Some(found)=hit(child,*rect,column,row,path,depth+1){return Some(found);}
                }
            }
            for index in 0..children.len().saturating_sub(1){
                let first=regions[index];let second=regions[index+1];
                let on=if *orientation==Split::Vertical{row>=body.y && row<body.bottom() && (column==first.right().saturating_sub(1)||column==second.x)}
                    else{column>=body.x && column<body.right() && (row==first.bottom().saturating_sub(1)||row==second.y)};
                if on{return Some(Divider{path,depth,index});}
            }
            None
        }
        hit(self,body,column,row,[0;MAX_DEPTH],0)
    }
    fn minimum(&self,axis:Split,width:u16,height:u16)->u16{
        match self{
            Self::Group(_)=>if axis==Split::Vertical{width}else{height},
            Self::Split{orientation,children,..}=>{
                let sizes=children.iter().map(|child|child.minimum(axis,width,height));
                if *orientation==axis{sizes.fold(0,u16::saturating_add)}else{sizes.max().unwrap_or(0)}
            }
        }
    }
    pub fn resize_divider(&mut self,body:Rect,divider:Divider,column:u16,row:u16,min_width:u16,min_height:u16)->bool{
        fn adjust(tree:&mut Tree,body:Rect,divider:Divider,level:usize,column:u16,row:u16,width:u16,height:u16)->bool{
            let Tree::Split{orientation,children,weights}=tree else{return false;};
            let regions=Tree::regions(body,*orientation,weights);
            if level<divider.depth{
                let index=usize::from(divider.path[level]);
                let Some(child)=children.get_mut(index) else{return false;};
                let Some(rect)=regions.get(index) else{return false;};
                return adjust(child,*rect,divider,level+1,column,row,width,height);
            }
            let index=divider.index;if index+1>=children.len(){return false;}
            let first=regions[index];let second=regions[index+1];
            let (origin,length,pointer)=if *orientation==Split::Vertical{(first.x,first.width.saturating_add(second.width),column)}else{(first.y,first.height.saturating_add(second.height),row)};
            let min_first=children[index].minimum(*orientation,width,height);
            let min_second=children[index+1].minimum(*orientation,width,height);
            if length<min_first.saturating_add(min_second){return false;}
            let wanted=pointer.saturating_sub(origin).clamp(min_first,length-min_second);
            let total=weights.iter().map(|weight|u32::from(*weight)).sum::<u32>().max(1);
            let factor=(10000/total).max(1);
            for weight in weights.iter_mut(){*weight=(u32::from(*weight)*factor)as u16;}
            let pair=u32::from(weights[index])+u32::from(weights[index+1]);
            let approximate=((pair*u32::from(wanted)+u32::from(length)/2)/u32::from(length.max(1))).clamp(1,pair-1);
            let mut best=None;
            for candidate in approximate.saturating_sub(4).max(1)..=approximate.saturating_add(4).min(pair-1){
                weights[index]=candidate as u16;weights[index+1]=(pair-candidate)as u16;
                let trial=Tree::regions(body,*orientation,weights);
                let first_size=if *orientation==Split::Vertical{trial[index].width}else{trial[index].height};
                let second_size=if *orientation==Split::Vertical{trial[index+1].width}else{trial[index+1].height};
                if first_size<min_first||second_size<min_second{continue;}
                let error=first_size.abs_diff(wanted);
                if best.is_none_or(|(_,previous)|error<previous){best=Some((candidate,error));}
            }
            let Some((weight,_))=best else{return false;};weights[index]=weight as u16;weights[index+1]=(pair-weight)as u16;true
        }
        let original=self.clone();
        if !adjust(self,body,divider,0,column,row,min_width,min_height){return false;}
        let groups=MAX_PANES;let rects=self.rects(body,groups);
        if rects.iter().filter(|rect|rect.width>0 ||rect.height>0).any(|rect|rect.width<min_width||rect.height<min_height){*self=original;return false;}
        *self!=original
    }
    pub fn split(&mut self, group: usize, new_group: usize, orientation: Split, depth: usize) -> bool {
        match self {
            Self::Group(index) if *index == group && depth < MAX_DEPTH => {
                *self = Self::Split { orientation, children: vec![Self::Group(group), Self::Group(new_group)], weights: vec![50,50] };
                true
            }
            Self::Split { children, .. } => children.iter_mut().any(|child| child.split(group, new_group, orientation, depth + 1)),
            _ => false,
        }
    }
    /// Drop one pane, collapse unary splits and renumber stable remaining
    /// indices. Session ownership and drafts are remapped separately by App.
    pub fn without(self, removed: usize) -> Option<Self> {
        match self {
            Self::Group(index) if index == removed => None,
            Self::Group(index) => Some(Self::Group(index - usize::from(index > removed))),
            Self::Split { orientation, children, weights } => {
                let mut kept = Vec::new(); let mut kept_weights = Vec::new();
                for (child, weight) in children.into_iter().zip(weights) {
                    if let Some(child) = child.without(removed) { kept.push(child); kept_weights.push(weight); }
                }
                if kept.len() == 1 { kept.pop() }
                else if kept.is_empty() { None }
                else { Some(Self::Split { orientation, children: kept, weights: kept_weights }) }
            }
        }
    }
    pub fn serialize(&self, groups: &[serde_json::Value]) -> serde_json::Value {
        match self {
            Self::Group(index) => groups.get(*index).cloned().unwrap_or(serde_json::Value::Null),
            Self::Split { orientation, children, weights } => {
                let total = weights.iter().map(|w| u32::from(*w)).sum::<u32>().max(1);
                serde_json::json!({"kind":"split", "orientation":if *orientation == Split::Vertical {"row"} else {"column"},
                    "children":children.iter().map(|child|child.serialize(groups)).collect::<Vec<_>>(),
                    "weights":weights.iter().map(|w|f64::from(*w)/f64::from(total)).collect::<Vec<_>>()})
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_geometry_and_removal_preserve_remaining_group_indices() {
        let mut tree=Tree::pair(Split::Vertical,40);
        assert!(tree.split(1,2,Split::Horizontal,0));
        let rects=tree.rects(Rect::new(0,0,100,40),3);
        assert_eq!(rects[0],Rect::new(0,0,40,40));
        assert_eq!(rects[1],Rect::new(40,0,60,20));
        assert_eq!(rects[2],Rect::new(40,20,60,20));
        let tree=tree.without(1).unwrap();
        let remaining=tree.rects(Rect::new(0,0,100,40),2);
        assert_eq!(remaining[1],Rect::new(40,0,60,40));
    }
    #[test]
    fn split_depth_is_bounded() {
        let mut tree=Tree::Group(0);
        for index in 1..=MAX_DEPTH {assert!(tree.split(0,index,Split::Vertical,0));}
        assert!(!tree.split(0,MAX_DEPTH+1,Split::Vertical,0));
    }
    #[test]
    fn nested_horizontal_and_vertical_drag_changes_only_adjacent_weights() {
        let body=Rect::new(0,0,180,80);
        let mut tree=Tree::pair(Split::Vertical,50);assert!(tree.split(1,2,Split::Horizontal,0));
        assert!(tree.divider_at(body,179,79).is_none());
        let horizontal=tree.divider_at(body,120,40).unwrap();
        assert!(tree.resize_divider(body,horizontal,120,55,28,8));
        let rects=tree.rects(body,3);assert_eq!(rects[0].width,90);assert_eq!(rects[1].height,55);
        let vertical=tree.divider_at(body,90,20).unwrap();
        assert!(tree.resize_divider(body,vertical,60,20,28,8));
        let rects=tree.rects(body,3);assert_eq!(rects[0].width,60);assert_eq!(rects[1].height,55);
        tree.resize_divider(body,vertical,0,20,28,8);
        assert!(tree.rects(body,3).iter().all(|r|r.width>=28&&r.height>=8));
    }

}
