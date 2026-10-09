//! Early Rust frontend for DOXA's 2.0 line.

pub mod markdown;
pub mod bridge;
pub mod remote_client;
mod remote_layout;
pub mod worker_frames;
pub mod history;
pub mod lore_picker;
pub mod lore_table;
pub mod memory_menu;
pub mod native_plugins;
pub mod model_fact_review;
pub mod model_catalog;
pub mod codegraph_snapshot;
pub mod diff_view;
pub mod discovery;
pub mod sessions;
pub mod transport;
pub mod ui;

pub mod ui_state;
pub mod collections;

pub mod launch;
pub mod isolation_migration;
pub mod operations;

pub mod peer_map;
pub mod fleet_view;
pub mod fleet_plan;
pub mod fleet_control;
pub mod theme;

pub mod mesh_control;
mod mesh_server;
pub mod maintenance;
pub mod settings;
pub mod keybindings;
pub mod preferences;
pub mod belief_graph;
pub mod belief_preview;

pub mod shell;
pub mod startup_restore;
pub mod first_run;
pub mod installation;

pub mod selection;
pub mod clipboard;
pub mod welcome;
