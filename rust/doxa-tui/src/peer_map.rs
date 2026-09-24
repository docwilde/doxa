//! Read-only, bounded peer presence and observed communication map.
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use serde_json::Value;
use std::collections::{HashMap, HashSet, VecDeque};
use tui_nodes::{Connection, NodeGraph, NodeLayout};

const MAX_PEERS: usize = 32;
const MAX_TITLE_CHARS: usize = 32;
const MAX_OBSERVATIONS: usize = 64;

#[derive(Clone, Debug)]
pub struct Peer {
    pub id: String,
    pub title: String,
    pub sent: u32,
    pub received: u32,
}

#[derive(Clone, Debug, Default)]
struct Scope {
    peers: Vec<Peer>,
    observations: VecDeque<(String, bool)>,
    available: bool,
    reason: String,
    rejected: usize,
}

#[derive(Clone, Debug, Default)]
pub struct PeerMap {
    scopes: HashMap<String, Scope>,
    pub selected: usize,
}

fn safe_id(value: &str) -> bool {
    doxa_state::valid_session_id(value)
}
fn label(value: &str) -> String {
    let clean = crate::markdown::sanitize(value).replace(['\n', '\r'], " ");
    clean.chars().take(MAX_TITLE_CHARS).collect()
}
impl PeerMap {
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
        self.selected = self.selected.min(scope.peers.len().saturating_sub(1));
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
                if scope.peers.iter().any(|peer| peer.id == id) {
                    return false;
                }
                if scope.peers.len() >= MAX_PEERS {
                    return false;
                }
                let title = data
                    .get("title")
                    .and_then(Value::as_str)
                    .map(label)
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| id.chars().take(12).collect());
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
                self.selected = self.selected.min(scope.peers.len().saturating_sub(1));
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
                    .take(MAX_PEERS)
                    .filter_map(Value::as_str)
                    .filter(|id| safe_id(id) && *id != owner && seen.insert(*id))
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
        self.selected = (self.selected as i32 + delta).clamp(0, count as i32 - 1) as usize;
    }

    pub fn render(&self, frame: &mut Frame, area: Rect, owner: &str) {
        let width = area.width.saturating_sub(4).min(106);
        let height = area.height.saturating_sub(2).min(34);
        if width < 20 || height < 6 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Block::default()
                .title(" Peer communications · Ctrl+M/Esc close · R refresh ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Cyan)),
            modal,
        );
        let inside = Rect::new(
            modal.x + 2,
            modal.y + 1,
            modal.width.saturating_sub(4),
            modal.height.saturating_sub(2),
        );
        let Some(scope) = self.scopes.get(owner) else {
            frame.render_widget(
                Paragraph::new("Peer map loading · request a refresh with R"),
                inside,
            );
            return;
        };
        if !scope.available {
            frame.render_widget(Paragraph::new(scope.reason.as_str()), inside);
            return;
        }
        if scope.peers.is_empty() {
            frame.render_widget(
                Paragraph::new("No live peers reported in this session's scope."),
                inside,
            );
            return;
        }
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Min(4), Constraint::Length(4)])
            .split(inside);
        let visible = scope.peers.len().min(6);
        let start = self.selected.saturating_sub(visible.saturating_sub(1));
        let shown = &scope.peers[start..(start + visible).min(scope.peers.len())];
        if rows[0].width >= 48 && rows[0].height >= 12 {
            render_graph(frame, rows[0], shown, self.selected - start);
        } else {
            let listing = shown
                .iter()
                .enumerate()
                .map(|(index, peer)| {
                    format!(
                        "{} {} · {}",
                        if start + index == self.selected {
                            "▸"
                        } else {
                            " "
                        },
                        peer.title,
                        &peer.id[..peer.id.len().min(8)]
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            frame.render_widget(Paragraph::new(listing).wrap(Wrap { trim: true }), rows[0]);
        }
        let peer = &scope.peers[self.selected.min(scope.peers.len() - 1)];
        let detail = format!("{} · {}\nObserved: {} sent / {} received · last {} events\n↑/↓ select peer · lines show observed traffic, not delivery guarantees",
            peer.title, peer.id, peer.sent, peer.received, scope.observations.len());
        frame.render_widget(
            Paragraph::new(detail).style(Style::default().fg(Color::Gray)),
            rows[1],
        );
    }
}

fn observe(scope: &mut Scope, id: &str, title: &str, outbound: bool) -> bool {
    let index = scope.peers.iter().position(|peer| peer.id == id);
    let index = match index {
        Some(index) => index,
        None if scope.peers.len() < MAX_PEERS => {
            scope.peers.push(Peer {
                id: id.into(),
                title: title.into(),
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

fn render_graph(frame: &mut Frame, area: Rect, peers: &[Peer], selected: usize) {
    let mut titles = vec!["This session".to_owned()];
    titles.extend(peers.iter().map(|peer| peer.title.clone()));
    let nodes: Vec<_> = titles
        .iter()
        .enumerate()
        .map(|(index, title)| {
            let color = if index == selected + 1 {
                Color::Yellow
            } else if index == 0 {
                Color::Cyan
            } else if peers[index - 1].sent > 0 || peers[index - 1].received > 0 {
                Color::Green
            } else {
                Color::DarkGray
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
    frame.render_stateful_widget(graph, area, &mut ());
}
