"""doxa.settings -- the settings modal (Ctrl+, / ``/settings`` / palette).

Three rules the panel exists to honor:

1. **Every row shows its EFFECTIVE value**, resolved through the documented
   precedence (env > ``~/.doxa/config.toml`` > default) and read fresh each
   time the modal opens. Never a hardcoded default, never a snapshot taken
   at app start -- a settings screen that shows something other than what
   is in force is worse than no settings screen.
2. **Every row says where its value came from** (``env DOXA_X — overrides
   config`` / ``config`` / ``default``). A value the user cannot change
   from the UI must be visibly explained, not mysteriously ignored.
3. **An env-shadowed row is read-only.** Editing it would write a config
   value the environment keeps shadowing -- the silent-no-op trap. The row
   is dimmed, marked ``(set by env)``, and offers no field at all.

Rows are grouped into category tabs (Session / Memory / Appearance /
Notifications / Paths / About) because one flat list stopped being readable
at ten rows. Category
switching is ``shift+left`` / ``shift+right``, deliberately NOT the app's
own tab keys -- a modal must never move the window's tabs underneath
itself. Unsaved edits SURVIVE a category switch (every pane stays mounted);
they are written on save and discarded on Esc.

Every row is a knob that already does something, named next to the code
that reads it. There are no placeholder settings: a menu that lists an
inert toggle teaches the user that the menu lies, and the next real toggle
is then not believed either.

Nothing in this panel animates -- Statics and Inputs, no Select popups, no
Switch -- because the chrome obeys the same no-idle-timers rule the status
line does.
"""

from __future__ import annotations

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TabbedContent, TabPane

from . import config as config_mod

CATEGORIES: tuple[str, ...] = (
    "Session", "Memory", "Appearance", "Notifications", "Paths", "About",
)


def field_id(key: str) -> str:
    return f"setting-{key}"


def resolved_value(setting: config_mod.Setting) -> str:
    """The value to SHOW for a row. Display-only rows resolve their real
    path from the code that owns it, so "read-only" never means "stale
    literal from a docstring"."""
    if setting.env == "LORE_ROOT":
        import lore_core

        return str(lore_core.ROOT)
    if setting.env == "DOXA_HOME":
        return str(config_mod.doxa_home())
    if setting.env == "DOXA_RUNTIME_DIR":
        from .peers import runtime_dir

        return str(runtime_dir())
    _source, value = config_mod.provenance(setting.env)
    return value


class SettingsScreen(ModalScreen["bool"]):
    """Dismisses True when anything was saved, False otherwise."""

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("ctrl+s", "save", "Save"),
        # Category navigation that cannot collide with the app's tab keys.
        Binding("shift+left", "prev_category", "Previous category", show=False),
        Binding("shift+right", "next_category", "Next category", show=False),
    ]

    def __init__(
        self,
        session_model: "str | None" = None,
        account: "dict | None" = None,
    ) -> None:
        super().__init__()
        self.session_model = session_model
        self.account = account or {}
        self.saved = False

    # -- composition --------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            with Horizontal(id="settings-header"):
                yield Static(
                    "▎ settings — env > config file > default", id="settings-title"
                )
                yield Static("✕", id="settings-close", classes="panel-close")
            with TabbedContent(id="settings-categories"):
                for category in CATEGORIES:
                    with TabPane(category, id=f"settings-cat-{category.lower()}"):
                        with VerticalScroll(classes="settings-rows"):
                            yield from self._category_rows(category)
            yield Static(
                "enter / ctrl+s: save · esc: close · shift+←/→: category",
                id="settings-hint",
            )

    def _category_rows(self, category: str) -> ComposeResult:
        if category == "About":
            yield from self._about_rows()
            return
        rows = [s for s in config_mod.SETTINGS if s.category == category]
        for setting in rows:
            yield from self._row(setting)
        if category == "Paths":
            yield from self._path_row(
                "config file", str(config_mod.config_path()),
                "Written by this modal; plain TOML, 0600, safe to hand-edit.",
            )

    def _row(self, setting: config_mod.Setting) -> ComposeResult:
        source, _stored = config_mod.provenance(setting.env)
        value = resolved_value(setting)
        env_forced = source == "env"
        shown = value or "(unset)"
        label = f"{setting.label}"
        if setting.env == "DOXA_MODEL" and self.session_model:
            # The model row follows the SESSION, which /model can move
            # underneath the config default.
            shown = self.session_model
            source = "session"
            if value and value != self.session_model:
                source = f"session — config default is {value}"
        yield Static(label, classes="setting-label")
        yield Static(
            f"{shown}   ({config_mod.source_label(setting.env) if source in ('env',) else source})",
            classes="setting-value" + (" setting-shadowed" if env_forced else ""),
        )
        if setting.read_only or not setting.key:
            pass  # display-only row: no field at all
        elif env_forced:
            # The silent-no-op trap, refused out loud.
            yield Static(
                f"(set by env — editing here would be shadowed; unset "
                f"{setting.env} to use the config file)",
                classes="setting-shadowed",
            )
        else:
            yield Input(
                value=self._stored_string(setting),
                placeholder=setting.placeholder(),
                id=field_id(setting.key),
                classes="setting-field",
            )
        yield Static(setting.help, classes="setting-help")
        if setting.note:
            yield Static(setting.note, classes="setting-note")

    def _path_row(self, label: str, value: str, note: str) -> ComposeResult:
        yield Static(label, classes="setting-label")
        yield Static(f"{value}   (resolved)", classes="setting-value")
        yield Static(note, classes="setting-help")

    def _about_rows(self) -> ComposeResult:
        from . import __version__
        from . import identity as identity_mod

        local = identity_mod.local_account()
        yield Static("version", classes="setting-label")
        yield Static(f"DOXA {__version__}", classes="setting-value")
        yield Static("Run /update to pull and apply the latest release.",
                     classes="setting-help")
        email = self.account.get("email") or local.get("emailAddress")
        if email:
            yield Static("account", classes="setting-label")
            yield Static(str(email), classes="setting-value")
        tier = identity_mod.account_tier(self.account, local)
        if tier:
            yield Static("plan", classes="setting-label")
            yield Static(tier, classes="setting-value")
            yield Static(
                "Precise tier from the CLI's own local config when it has "
                "one; the SDK's subscriptionType otherwise.",
                classes="setting-help",
            )
        org = identity_mod.organization(self.account, local)
        if org:
            yield Static("organization", classes="setting-label")
            yield Static(str(org), classes="setting-value")
            yield Static(
                "Informative only — an organization is never the plan.",
                classes="setting-help",
            )

    @staticmethod
    def _stored_string(setting: config_mod.Setting) -> str:
        stored = config_mod.load().get(setting.key, "")
        if isinstance(stored, bool):
            return "1" if stored else ""
        return "" if stored is None else str(stored)

    # -- editing ------------------------------------------------------

    def editable(self) -> list[config_mod.Setting]:
        """Rows that actually offer a field: writable, and not shadowed by
        an environment variable."""
        return [
            s for s in config_mod.SETTINGS
            if s.key and not s.read_only
            and not config_mod.overridden_by_env(s.env)
        ]

    def values(self) -> dict[str, str]:
        return {
            setting.key: self.query_one(f"#{field_id(setting.key)}", Input).value
            for setting in self.editable()
        }

    def action_cancel(self) -> None:
        self.dismiss(self.saved)

    def action_save(self) -> None:
        """Write, then RE-READ: the panel redraws from the file it just
        wrote, so what it shows afterwards is the new effective value --
        which for an env-shadowed knob still means the env value wins."""
        config_mod.save(self.values())
        self.saved = True
        self.refresh(recompose=True)

    def _category_step(self, delta: int) -> None:
        tabs = self.query_one("#settings-categories", TabbedContent)
        ids = [f"settings-cat-{c.lower()}" for c in CATEGORIES]
        try:
            index = ids.index(tabs.active)
        except ValueError:
            index = 0
        tabs.active = ids[(index + delta) % len(ids)]

    def action_prev_category(self) -> None:
        self._category_step(-1)

    def action_next_category(self) -> None:
        self._category_step(1)

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
