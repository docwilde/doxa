"""doxa.settings -- the settings modal (Ctrl+, / ``/settings`` / palette).

Every row is a knob that already does something, named next to the code
that reads it (see :data:`doxa.config.SETTINGS`). There are no placeholder
rows: a settings menu that lists an inert toggle teaches the user that the
menu lies, and the next real toggle is then not believed either.

The modal is deliberately plain -- a label, an editable field, a help line
per row. No Select popups, no Switches: nothing here animates, because the
one property this app's status line advertises (containment signal, not
decoration) applies to its chrome too, and every animated widget is another
timer left armed on an idle terminal.

The precedence rule (env > file > default) is not hidden behind the UI:
a row whose environment variable is set is marked ``env`` and says what the
environment is forcing, so an edit that cannot take effect says so instead
of silently doing nothing.

Same modal shape as ``doxa.history``: a ModalScreen over the dimmed screen
wash, Esc closes, and it dismisses with a value the app acts on (here:
True when settings were saved, so the caller can re-read what changed).
"""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from . import config as config_mod


def field_id(key: str) -> str:
    return f"setting-{key}"


class SettingsScreen(ModalScreen["bool"]):
    """Dismisses True when the file was written, False when cancelled."""

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("ctrl+s", "save", "Save"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            with Horizontal(id="settings-header"):
                yield Static(
                    "▎ settings — env > config file > default", id="settings-title"
                )
                # Mouse users get a target, not just a key: the panel-close
                # convention this app now uses everywhere it has a panel.
                yield Static("✕", id="settings-close", classes="panel-close")
            with VerticalScroll(id="settings-rows"):
                for setting in config_mod.SETTINGS:
                    yield from self._row(setting)
            yield Static(
                f"stored in {config_mod.config_path()}", id="settings-path"
            )
            yield Static(
                "enter / ctrl+s: save · esc: cancel · empty field = default",
                id="settings-hint",
            )

    def _row(self, setting: config_mod.Setting) -> ComposeResult:
        env_forced = config_mod.overridden_by_env(setting.env)
        marker = "  [env]" if env_forced else ""
        yield Static(f"{setting.label}{marker}", classes="setting-label")
        if setting.read_only or not setting.key:
            yield Static(
                config_mod.effective(setting.env) or "(unset)",
                classes="setting-readonly",
            )
        else:
            stored = config_mod.load().get(setting.key, "")
            if isinstance(stored, bool):
                stored = "1" if stored else ""
            yield Input(
                value="" if stored is None else str(stored),
                placeholder=setting.placeholder(),
                id=field_id(setting.key),
                classes="setting-field",
            )
        help_text = setting.help
        if env_forced:
            help_text = (
                f"{setting.env} is set in the environment and wins: "
                f"{config_mod.effective(setting.env)!r} — unset it for this "
                "field to take effect. " + help_text
            )
        yield Static(help_text, classes="setting-help")

    def values(self) -> dict[str, str]:
        return {
            setting.key: self.query_one(f"#{field_id(setting.key)}", Input).value
            for setting in config_mod.SETTINGS
            if setting.key and not setting.read_only
        }

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_save(self) -> None:
        config_mod.save(self.values())
        self.dismiss(True)

    @on(Input.Submitted)
    def _on_submit(self, event: Input.Submitted) -> None:
        """Enter in any field saves the whole form -- and must never bubble
        to the pane's prompt-submit handler behind the modal."""
        event.stop()
        self.action_save()

    @on(events.Click, "#settings-close")
    def _on_close_clicked(self, event: events.Click) -> None:
        event.stop()
        self.action_cancel()
