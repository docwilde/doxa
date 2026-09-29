//! Read-only, bounded peer presence and observed communication map.
use ratatui::layout::{Position, Rect};
use ratatui::style::{Modifier, Style};
use crate::theme;
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::text::Line;
use ratatui::Frame;
use serde_json::Value;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::time::{Duration, Instant};
use crossterm::event::{MouseButton, MouseEventKind};
use tui_nodes::{Connection, NodeGraph, NodeLayout};

const MAX_PEERS: usize = 32;
const MAX_TITLE_CHARS: usize = 32;
const MAX_OBSERVATIONS: usize = 64;
const MAX_MESSAGES: usize = 50;
const HOVER_DELAY: Duration = Duration::from_millis(500);

#[derive(Clone, Debug)]
pub struct Peer {
    pub id: String,
    pub title: String,
    pub sent: u32,
    pub received: u32,
}

#[derive(Clone, Debug)]
struct Message {
    id: String,
    from: String,
    to: Vec<String>,
    ts: String,
    body: String,
}

#[derive(Clone, Debug)]
struct Hover {
    id: String,
    peer: String,
    since: Instant,
    visible: bool,
    scroll: u16,
}

#[derive(Clone, Debug, Default)]
struct Scope {
    peers: Vec<Peer>,
    observations: VecDeque<(String, bool)>,
    available: bool,
    reason: String,
    rejected: usize,
    messages: Vec<Message>,
    history_reason: String,
}

#[derive(Clone, Debug, Default)]
pub struct PeerMap {
    scopes: HashMap<String, Scope>,
    pub selected: usize,
    message_scroll: usize,
    focus_messages: bool,
    hover: Option<Hover>,
    graph_hits: RefCell<Option<(String, Vec<(Rect, usize)>)>>,
}

fn safe_id(value: &str) -> bool {
    doxa_state::valid_session_id(value)
}
fn label(value: &str) -> String {
    let clean = crate::markdown::sanitize(value).replace(['\n', '\r'], " ");
    clean.chars().take(MAX_TITLE_CHARS).collect()
}

impl Message {
    fn involves(&self, owner: &str, peer: &str) -> bool {
        (self.from == owner && self.to.iter().any(|id| id == peer))
            || (self.from == peer && self.to.iter().any(|id| id == owner))
    }
}

#[derive(Clone, Copy)]
struct MapRects {
    modal: Rect,
    graph: Option<Rect>,
    peers: Rect,
    messages: Rect,
}

fn map_rects(area: Rect) -> Option<MapRects> {
    let width = area.width.saturating_sub(4).min(106);
    let height = area.height.saturating_sub(2).min(34);
    if width < 20 || height < 6 { return None; }
    let modal = Rect::new(area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2, width, height);
    let inside = Rect::new(modal.x + 2, modal.y + 1,
        modal.width.saturating_sub(4), modal.height.saturating_sub(2));
    let graph_height = if inside.width >= 60 && inside.height >= 20 { 12 } else { 0 };
    let graph = (graph_height > 0).then(|| Rect::new(inside.x, inside.y, inside.width, graph_height));
    let bottom = Rect::new(inside.x, inside.y + graph_height, inside.width, inside.height - graph_height);
    let peer_width = (bottom.width / 3).clamp(8, 30).min(bottom.width.saturating_sub(10));
    Some(MapRects {
        modal, graph,
        peers: Rect::new(bottom.x, bottom.y, peer_width, bottom.height),
        messages: Rect::new(bottom.x + peer_width, bottom.y, bottom.width - peer_width, bottom.height),
    })
}

fn content_rect(area: Rect) -> Rect {
    Rect::new(area.x.saturating_add(1), area.y.saturating_add(1),
        area.width.saturating_sub(2), area.height.saturating_sub(2))
}

fn tooltip_rect(rects: MapRects) -> Rect {
    let width = rects.messages.width.min(80).max(1);
    let height = rects.modal.height.saturating_sub(4).min(14).max(2);
    Rect::new(rects.messages.x,
        rects.messages.y.saturating_sub(height).max(rects.modal.y + 1),
        width, height)
}
impl PeerMap {
    pub fn history(&mut self, owner: &str, frame: &Value) -> bool {
        if !safe_id(owner) { return false; }
        let scope = self.scopes.entry(owner.to_owned()).or_default();
        scope.messages.clear();
        self.message_scroll = 0;
        self.hover = None;
        if frame["ok"] != true {
            scope.history_reason = "Peer history unavailable".into();
            return true;
        }
        let Some(rows) = frame["messages"].as_array().filter(|rows| rows.len() <= MAX_MESSAGES) else {
            scope.history_reason = "Peer history returned malformed data".into();
            return true;
        };
        for row in rows {
            let Some(id) = row["id"].as_str().filter(|id| !id.is_empty() && id.len() <= 128
                && !id.chars().any(char::is_control)) else { continue };
            let Some(from) = row["from"]["session"].as_str().filter(|id| safe_id(id)) else { continue };
            let Some(to) = row["to"].as_array().filter(|to| to.len() <= MAX_PEERS) else { continue };
            let to: Vec<String> = to.iter().filter_map(Value::as_str).filter(|id| safe_id(id))
                .map(str::to_owned).collect();
            if from != owner && !to.iter().any(|id| id == owner) { continue; }
            let Some(body) = row["body"].as_str().filter(|body| body.chars().count() <= 8_000) else { continue };
            scope.messages.push(Message {
                id: id.to_owned(), from: from.to_owned(), to,
                ts: label(row["ts"].as_str().unwrap_or("")),
                body: crate::markdown::sanitize(body),
            });
        }
        scope.history_reason.clear();
        true
    }

    pub fn roster(&mut self, owner: &str, frame: &Value) -> bool {
        if !safe_id(owner) {
            return false;
        }
        let scope = self.scopes.entry(owner.to_owned()).or_default();
        if frame.get("ok").and_then(Value::as_bool) != Some(true) {
            scope.available = false;
            scope.reason = "Peer discovery unavailable on this daemon".into();
            scope.peers.clear();
            return true;
        }
        let Some(rows) = frame.get("peers").and_then(Value::as_array) else {
            scope.available = false;
            scope.reason = "Peer discovery returned malformed data".into();
            scope.peers.clear();
            return true;
        };
        if rows.len() > MAX_PEERS {
            scope.available = false;
            scope.reason = "Peer roster exceeds display limit".into();
            scope.peers.clear();
            return true;
        }
        let mut peers = Vec::new();
        let mut rejected = 0;
        for row in rows {
            let Some(id) = row
                .get("session_id")
                .and_then(Value::as_str)
                .filter(|id| safe_id(id) && *id != owner)
            else {
                rejected += 1;
                continue;
            };
            if peers.iter().any(|peer: &Peer| peer.id == id) {
                rejected += 1;
                continue;
            }
            let title = row
                .get("title")
                .and_then(Value::as_str)
                .map(label)
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| id.chars().take(12).collect());
            let old = scope.peers.iter().find(|peer| peer.id == id);
            peers.push(Peer {
                id: id.into(),
                title,
                sent: old.map_or(0, |p| p.sent),
                received: old.map_or(0, |p| p.received),
            });
        }
        scope.peers = peers;
        scope.rejected = rejected;
        scope.available = rows.is_empty() || !scope.peers.is_empty();
        scope.reason = if scope.available {
            String::new()
        } else {
            "Peer discovery returned no valid entries".into()
        };
        true
    }

    pub fn event(&mut self, owner: &str, kind: &str, data: &Value) -> bool {
        if !safe_id(owner) {
            return false;
        }
        let scope = self.scopes.entry(owner.to_owned()).or_default();
        match kind {
            "peer_joined" => {
                let Some(id) = data
                    .get("session_id")
                    .and_then(Value::as_str)
                    .filter(|id| safe_id(id) && *id != owner)
                else {
                    return false;
                };
                let title = data
                    .get("title")
                    .and_then(Value::as_str)
                    .map(label)
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| id.chars().take(12).collect());
                if let Some(peer) = scope.peers.iter_mut().find(|peer| peer.id == id) {
                    if peer.title == title {
                        return false;
                    }
                    peer.title = title;
                    return true;
                }
                if scope.peers.len() >= MAX_PEERS {
                    return false;
                }
                scope.peers.push(Peer {
                    id: id.into(),
                    title,
                    sent: 0,
                    received: 0,
                });
                scope.available = true;
                true
            }
            "peer_left" => {
                let Some(id) = data
                    .get("session_id")
                    .and_then(Value::as_str)
                    .filter(|id| safe_id(id))
                else {
                    return false;
                };
                let before = scope.peers.len();
                scope.peers.retain(|peer| peer.id != id);
                scope.observations.retain(|(peer, _)| peer != id);
                before != scope.peers.len()
            }
            "peer_message" => {
                let Some(id) = data
                    .get("from_id")
                    .and_then(Value::as_str)
                    .filter(|id| safe_id(id) && *id != owner)
                else {
                    return false;
                };
                let title = data
                    .get("from_title")
                    .and_then(Value::as_str)
                    .map(label)
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| id.chars().take(12).collect());
                observe(scope, id, &title, false)
            }
            "peer_sent" => {
                let Some(ids) = data.get("to").and_then(Value::as_array) else {
                    return false;
                };
                let mut changed = false;
                let mut seen = HashSet::new();
                for id in ids
                    .iter()
                    .filter_map(Value::as_str)
                    .filter(|id| safe_id(id) && *id != owner && seen.insert(*id))
                    .take(MAX_PEERS)
                {
                    changed |= observe(scope, id, id, true);
                }
                changed
            }
            _ => false,
        }
    }

    pub fn move_selected(&mut self, owner: &str, delta: i32) {
        let count = self.scopes.get(owner).map_or(0, |scope| scope.peers.len());
        if count == 0 {
            self.selected = 0;
            return;
        }
        let next = (self.selected as i32 + delta).clamp(0, count as i32 - 1) as usize;
        if next != self.selected {
            self.selected = next;
            self.message_scroll = 0;
            self.hover = None;
        }
    }

    pub fn toggle_focus(&mut self) {
        self.focus_messages = !self.focus_messages;
        self.hover = None;
    }

    pub fn focus_messages(&self) -> bool { self.focus_messages }

    pub fn scroll_messages(&mut self, owner: &str, delta: i32) -> bool {
        let Some(scope) = self.scopes.get(owner) else { return false };
        let Some(peer) = scope.peers.get(self.selected.min(scope.peers.len().saturating_sub(1))) else { return false };
        let count = scope.messages.iter().filter(|message| message.involves(owner, &peer.id)).count();
        let next = (self.message_scroll as i32 + delta).clamp(0, count.saturating_sub(1) as i32) as usize;
        let changed = next != self.message_scroll;
        self.message_scroll = next;
        self.hover = None;
        changed
    }

    pub fn tick(&mut self, now: Instant) -> bool {
        let Some(hover) = self.hover.as_mut() else { return false };
        if !hover.visible && now.saturating_duration_since(hover.since) >= HOVER_DELAY {
            hover.visible = true;
            return true;
        }
        false
    }

    pub fn mouse(&mut self, area: Rect, owner: &str, column: u16, row: u16,
        kind: MouseEventKind, now: Instant) -> bool {
        let Some(rects) = map_rects(area) else { return false };
        let point = Position::new(column, row);
        if !rects.modal.contains(point) {
            return self.hover.take().is_some();
        }
        if kind == MouseEventKind::Down(MouseButton::Left) {
            let selected = self.graph_hits.borrow().as_ref()
                .filter(|(painted_owner, _)| painted_owner == owner)
                .and_then(|(_, hits)| hits.iter().find(|(rect, _)| rect.contains(point)).map(|(_, index)| *index));
            if let Some(index) = selected {
                let changed = self.selected != index;
                self.selected = index;
                self.message_scroll = 0;
                self.hover = None;
                return changed;
            }
        }
        if let Some(hover) = self.hover.as_mut().filter(|hover| hover.visible) {
            if tooltip_rect(rects).contains(point) {
                match kind {
                    MouseEventKind::ScrollUp => { hover.scroll = hover.scroll.saturating_sub(2); return true; }
                    MouseEventKind::ScrollDown => { hover.scroll = hover.scroll.saturating_add(2); return true; }
                    MouseEventKind::Moved => return false,
                    _ => {}
                }
            }
        }
        let Some(scope) = self.scopes.get(owner) else { return false };
        if scope.peers.is_empty() { return false }
        let selected = self.selected.min(scope.peers.len() - 1);
        let peer_area = content_rect(rects.peers);
        if peer_area.contains(point) {
            if kind == MouseEventKind::Down(MouseButton::Left) {
                let visible = usize::from(peer_area.height).max(1);
                let first = selected.saturating_sub(visible.saturating_sub(1));
                let index = first + usize::from(row - peer_area.y);
                if index < scope.peers.len() && index != self.selected {
                    self.selected = index;
                    self.message_scroll = 0;
                    self.hover = None;
                    return true;
                }
            }
            if matches!(kind, MouseEventKind::ScrollUp | MouseEventKind::ScrollDown) {
                self.move_selected(owner, if kind == MouseEventKind::ScrollUp { -1 } else { 1 });
                return true;
            }
        }
        let message_area = content_rect(rects.messages);
        if message_area.contains(point) {
            if matches!(kind, MouseEventKind::ScrollUp | MouseEventKind::ScrollDown) {
                return self.scroll_messages(owner, if kind == MouseEventKind::ScrollUp { 1 } else { -1 });
            }
            if matches!(kind, MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left)) {
                let peer = &scope.peers[selected];
                let messages: Vec<_> = scope.messages.iter().filter(|message| message.involves(owner, &peer.id)).collect();
                let viewport = usize::from(message_area.height).max(1);
                let offset = self.message_scroll.min(messages.len().saturating_sub(viewport));
                let end = messages.len().saturating_sub(offset);
                let first = end.saturating_sub(viewport);
                let index = first + usize::from(row - message_area.y);
                let next = messages.get(index).filter(|_| index < end)
                    .map(|message| (message.id.clone(), peer.id.clone()));
                if self.hover.as_ref().map(|hover| (&hover.id, &hover.peer))
                    != next.as_ref().map(|(id, peer)| (id, peer)) {
                    self.hover = next.map(|(id, peer)| Hover { id, peer, since: now, visible: false, scroll: 0 });
                    return true;
                }
            }
        } else if kind == MouseEventKind::Moved {
            return self.hover.take().is_some();
        }
        false
    }

    pub fn render(&self, frame: &mut Frame, area: Rect, owner: &str) {
        self.graph_hits.borrow_mut().take();
        let Some(rects) = map_rects(area) else { return };
        frame.render_widget(Clear, rects.modal);
        frame.render_widget(
            Block::default()
                .title(" Peer communications · Tab focus · R refresh · Esc close ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme::BORDER))
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            rects.modal,
        );
        let Some(scope) = self.scopes.get(owner) else {
            frame.render_widget(Paragraph::new("Peer map loading · request a refresh with R"), content_rect(rects.modal));
            return;
        };
        if !scope.available {
            let reason = if scope.reason.is_empty() { "Peer map loading · request a refresh with R" }
                else { scope.reason.as_str() };
            frame.render_widget(Paragraph::new(reason), content_rect(rects.modal));
            return;
        }
        if scope.peers.is_empty() {
            frame.render_widget(Paragraph::new("No live peers reported in this session's scope."), content_rect(rects.modal));
            return;
        }
        let selected = self.selected.min(scope.peers.len() - 1);
        if let Some(graph) = rects.graph {
            let start = selected.saturating_sub(2);
            let shown = &scope.peers[start..(start + 3).min(scope.peers.len())];
            let hits = render_graph(frame, graph, shown, selected - start)
                .into_iter().map(|(rect, index)| (rect, start + index)).collect();
            *self.graph_hits.borrow_mut() = Some((owner.to_owned(), hits));
        }
        let peer_area = content_rect(rects.peers);
        let visible = usize::from(peer_area.height).max(1);
        let start = selected.saturating_sub(visible.saturating_sub(1));
        let mut peer_lines = Vec::new();
        for (index, peer) in scope.peers.iter().enumerate().skip(start).take(visible) {
            let prefix = if index == selected { "▸" } else { " " };
            let text = format!("{prefix} {} · {}", peer.title, &peer.id[..peer.id.len().min(8)]);
            let style = if index == selected { Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT) }
                else { Style::default().fg(theme::SECONDARY) };
            peer_lines.push(Line::styled(text, style));
        }
        frame.render_widget(Paragraph::new(peer_lines).block(Block::default().borders(Borders::ALL)
            .title(if self.focus_messages { " Peers · click to select " } else { " Peers · selected " })
            .border_style(Style::default().fg(theme::BORDER))), rects.peers);

        let peer = &scope.peers[selected];
        let messages: Vec<&Message> = scope.messages.iter().filter(|row| row.involves(owner, &peer.id)).collect();
        let message_area = content_rect(rects.messages);
        let viewport = usize::from(message_area.height).max(1);
        let offset = self.message_scroll.min(messages.len().saturating_sub(viewport));
        let end = messages.len().saturating_sub(offset);
        let first = end.saturating_sub(viewport);
        let mut message_lines = Vec::new();
        for message in &messages[first..end] {
            let arrow = if message.from == owner { "→" } else { "←" };
            let clock: String = message.ts.chars().skip(11).take(5).collect();
            let preview = message.body.lines().next().unwrap_or("");
            let text = format!("{arrow} {clock} {preview}");
            let text: String = text.chars().take(usize::from(message_area.width)).collect();
            let hovered = self.hover.as_ref().is_some_and(|hover| hover.id == message.id && hover.peer == peer.id);
            let style = if hovered { Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT) }
                else { Style::default().fg(theme::SECONDARY) };
            message_lines.push(Line::styled(text, style));
        }
        if messages.is_empty() {
            message_lines.push(Line::from(if scope.history_reason.is_empty() {
                "No recent messages with this peer."
            } else { scope.history_reason.as_str() }));
        }
        let title = format!(" {} · {} sent / {} received · {} recent ", peer.title, peer.sent, peer.received, messages.len());
        frame.render_widget(Paragraph::new(message_lines).block(Block::default().borders(Borders::ALL)
            .title(title).border_style(Style::default().fg(theme::BORDER))), rects.messages);

        if let Some(hover) = self.hover.as_ref().filter(|hover| hover.visible && hover.peer == peer.id) {
            if let Some(message) = messages.iter().find(|row| row.id == hover.id) {
                let tooltip = tooltip_rect(rects);
                frame.render_widget(Clear, tooltip);
                frame.render_widget(Paragraph::new(message.body.as_str()).wrap(Wrap { trim: false })
                    .scroll((hover.scroll, 0)).block(Block::default().borders(Borders::ALL)
                        .title(" Full peer message · wheel scroll ")
                        .border_style(Style::default().fg(theme::ACCENT))
                        .style(Style::default().fg(theme::TEXT).bg(theme::RAISED))), tooltip);
            }
        }
    }

}

fn observe(scope: &mut Scope, id: &str, title: &str, outbound: bool) -> bool {
    let index = scope.peers.iter().position(|peer| peer.id == id);
    let index = match index {
        Some(index) => index,
        None if scope.peers.len() < MAX_PEERS => {
            let title = label(title);
            let title = if title.is_empty() { id.chars().take(12).collect() } else { title };
            scope.peers.push(Peer {
                id: id.into(),
                title,
                sent: 0,
                received: 0,
            });
            scope.peers.len() - 1
        }
        None => return false,
    };
    if outbound {
        scope.peers[index].sent = scope.peers[index].sent.saturating_add(1);
    } else {
        scope.peers[index].received = scope.peers[index].received.saturating_add(1);
    }
    if scope.observations.len() == MAX_OBSERVATIONS {
        scope.observations.pop_front();
    }
    scope.observations.push_back((id.into(), outbound));
    scope.available = true;
    true
}

fn render_graph(frame: &mut Frame, area: Rect, peers: &[Peer], selected: usize) -> Vec<(Rect, usize)> {
    let mut titles = vec!["This session".to_owned()];
    titles.extend(peers.iter().map(|peer| peer.title.clone()));
    let nodes: Vec<_> = titles
        .iter()
        .enumerate()
        .map(|(index, title)| {
            let color = if index == selected + 1 {
                theme::ACCENT
            } else if index == 0 {
                theme::BORDER
            } else if peers[index - 1].sent > 0 || peers[index - 1].received > 0 {
                theme::SUCCESS
            } else {
                theme::MUTED
            };
            NodeLayout::new((16, 3))
                .with_title(title)
                .with_border_style(
                    Style::default()
                        .fg(color)
                        .add_modifier(Modifier::BOLD),
                )
        })
        .collect();
    // One DAG edge per peer with observed traffic. The widget's layout is
    // directional, while the line itself means communication in either direction.
    let connections: Vec<_> = peers
        .iter()
        .enumerate()
        .filter(|(_, peer)| peer.sent > 0 || peer.received > 0)
        .map(|(index, _)| Connection::new(index + 1, 0, 0, 0))
        .collect();
    let mut graph = NodeGraph::new(
        nodes,
        connections,
        area.width as usize,
        area.height as usize,
    );
    graph.calculate();
    let hits = graph.split(area).into_iter().enumerate().skip(1)
        .filter_map(|(index, inner)| {
            if inner.width == 0 || inner.height == 0 { return None; }
            Some((Rect::new(inner.x.saturating_sub(1), inner.y.saturating_sub(1),
                inner.width.saturating_add(2), inner.height.saturating_add(2)), index - 1))
        }).collect();
    frame.render_stateful_widget(graph, area, &mut ());
    hits
}

#[cfg(test)]
mod interaction_tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};
    use serde_json::json;

    #[test]
    fn graph_nodes_select_peers_and_history_tooltips_wait_for_dwell() {
        let mut map = PeerMap::default();
        map.roster("owner-1", &json!({"ok":true,"peers":[
            {"session_id":"peer-1","title":"First"}, {"session_id":"peer-2","title":"Second"}]}));
        map.history("owner-1", &json!({"ok":true,"messages":[
            {"id":"one","from":{"session":"owner-1"},"to":["peer-1"],"ts":"2026-09-29T12:00:00Z","body":"Private to first"},
            {"id":"two","from":{"session":"peer-2"},"to":["owner-1"],"ts":"2026-09-29T12:01:00Z","body":"Full second peer message"}
        ]}));
        let area = Rect::new(0, 0, 100, 30);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| map.render(frame, area, "owner-1")).unwrap();
        let hit = map.graph_hits.borrow().as_ref().unwrap().1.iter().find(|(_, index)| *index == 1).unwrap().0;
        assert!(map.mouse(area, "owner-1", hit.x + 1, hit.y + 1,
            MouseEventKind::Down(MouseButton::Left), Instant::now()));
        assert_eq!(map.selected, 1);
        terminal.draw(|frame| map.render(frame, area, "owner-1")).unwrap();
        let messages = map_rects(area).unwrap().messages;
        let row = content_rect(messages);
        let now = Instant::now();
        assert!(map.mouse(area, "owner-1", row.x + 2, row.y, MouseEventKind::Moved, now));
        assert!(!map.tick(now + Duration::from_millis(499)));
        assert!(map.tick(now + Duration::from_millis(500)));
        terminal.draw(|frame| map.render(frame, area, "owner-1")).unwrap();
        let output = terminal.backend().buffer().content.iter().map(|cell|cell.symbol()).collect::<String>();
        assert!(output.contains("Full second peer message"));
        assert!(!output.contains("Private to first"));
    }
}
