//! New Session Docker checks and return-to-draft coordination.
use super::App;
pub(super) use crate::docker_setup::{GUIDE, Prerequisites};

impl App {
    pub(super) fn refresh_docker_setup(&mut self) {
        self.docker_setup = self.new_session.as_ref().filter(|form| form.isolation.docker())
            .map(|_| Prerequisites::inspect());
        if self.docker_setup.as_ref().is_some_and(Prerequisites::ready) {
            if let Some(form) = self.new_session.as_mut() {
                if form.launch_error.as_deref().is_some_and(|error| error.starts_with("Docker setup:")) {
                    form.launch_error = None;
                }
            }
        }
    }

    pub(super) fn open_docker_settings(&mut self) -> bool {
        if self.launching || !self.new_session.as_ref().is_some_and(|form| form.isolation.docker()) { return false; }
        // Keep the draft in place: Settings owns modal input/rendering, while
        // pending model catalog replies can still update the underlying form.
        self.open_settings_menu();
        let Some(menu) = self.settings_menu.as_mut() else { return true; };
        menu.category = crate::settings::CATEGORIES.iter().position(|category| *category == "Session").unwrap_or(0);
        menu.selected = menu.rows.iter().position(|row| row.setting.key == "docker_image").unwrap_or(0);
        self.docker_settings_open = true;
        self.notice = format!("Docker worker image digest · Ctrl+S saves · Esc returns to your draft · guide: {GUIDE}");
        true
    }

    pub(super) fn close_settings_menu(&mut self) {
        self.title_tab_hover = None;
        self.settings_menu = None;
        self.choice_dropdown = None;
        self.mcp_secret = None;
        if self.docker_settings_open {
            self.docker_settings_open = false;
            self.refresh_docker_setup();
            self.notice = "Returned to New Session · Docker choice, model and prompt retained".into();
        }
    }
}
