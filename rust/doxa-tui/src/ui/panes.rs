//! Bounded recursive group geometry. Session/tab records remain authoritative
//! in UiStateStore; this tree only names group indices and proportions.
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use super::Split;

pub const MAX_PANES: usize = 16;
pub const MAX_DEPTH: usize = 8;
pub const MAX_TABS: usize = 256;

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
}
