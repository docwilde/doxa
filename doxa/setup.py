"""doxa.setup -- ``/setup``: check state, fix findings ONE at a time.

Modeled on ``/lore:setup``'s shape (check -> fix -> confirm, one finding at
a time, never a batch of silent changes): every step SHOWS what is true
right now and, when there is something to change, exactly what applying it
would do -- before it happens. A step with nothing to do says so and moves
on; nothing here is a placeholder row (same discipline as
``doxa.config.SETTINGS``).

The findings are DATA (:class:`Finding`, :class:`Choice`), gathered by
:func:`collect_findings` off the event loop (it shells out to probe auth),
exactly like ``doxa.commands``' registry -- the Textual screen
(:class:`SetupScreen`, below) is a dumb walker over this list, so every
branch here is testable without a running TUI.

Auto-run vs on-demand: a marker file (:func:`marker_path`) tracks whether
DOXA has EVER launched on this machine. :func:`needs_first_run` is true
only until the very first launch marks it seen -- and that happens the
moment the wizard is offered, not when it finishes, so declining or
Esc-ing out of it can never make it reappear uninvited at the next launch.
``/setup`` (the command, the palette entry) always runs it again, on
purpose, any time.

The LORE store decision is the one step with real, sticky state: env
always wins (nothing to decide), a value already chosen by a previous run
is remembered (``config.toml``'s ``lore_root`` key, written through
:func:`doxa.config.save_lore_root` -- a dedicated writer because this row
is deliberately absent from the settings MODAL's editable fields, see
``doxa/settings.py``), and only the genuinely ambiguous case -- an
existing store the Claude Code LORE plugin already uses -- asks. Read
early (``doxa/_lore_bootstrap.py``) and exported to ``LORE_ROOT`` before
``lore_core`` is ever imported, because that module reads the environment
once, at ITS import time, not per call.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from . import auth as auth_mod
from . import commands as commands_mod
from . import config as config_mod

MARKER_NAME = ".setup-done"
PLUGIN_LORE_DIR = Path.home() / ".claude" / "lore"
DOXA_LORE_DIR_NAME = "lore"
AUTH_PROBE_TIMEOUT_SECS = 10.0

# Special Finding.action tags SetupScreen recognizes and handles itself
# instead of (or in addition to) a Choice's apply(). Kept as constants
# rather than magic strings scattered across two modules.
ACTION_OPEN_SETTINGS = "open-settings"


def marker_path() -> Path:
    return config_mod.doxa_home() / MARKER_NAME


def needs_first_run() -> bool:
    """True until DOXA has ever launched on this machine before.

    ``DOXA_SKIP_FIRST_RUN`` is a test-only kill switch (set suite-wide by
    tests/conftest.py, same discipline as ``LORE_DISABLE_REVIEW`` and
    ``DOXA_IMAGE_MODE=text``): plenty of tests point ``DOXA_HOME`` at
    their OWN fresh throwaway directory for isolation reasons that have
    nothing to do with "has doxa genuinely never run on this machine", and
    a marker file alone cannot tell those two cases apart."""
    if os.environ.get("DOXA_SKIP_FIRST_RUN", "").strip():
        return False
    return not marker_path().exists()


def mark_seen() -> None:
    """Consume the first-launch trigger. Called the moment the wizard is
    OFFERED (auto-triggered), not when it finishes -- a user who cancels
    out of it has still seen it; the marker must not cause it to nag again
    at the next launch. Never raises: a marker DOXA fails to write costs
    a re-prompt next time, never a crash."""
    path = marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.touch(exist_ok=True)
    except OSError:
        return


@dataclass(frozen=True)
class Choice:
    """One option a :class:`Finding` can be resolved with."""

    label: str
    """Short verb phrase: "use the plugin's store", "create it"."""

    detail: str
    """What applying it changes, spelled out -- shown before it happens."""

    apply: "Callable[[], str]"
    """Performs the change; returns the one-line result message."""


@dataclass(frozen=True)
class Finding:
    """One check, and everything the wizard needs to walk it."""

    id: str
    title: str
    state: str
    """What's true right now -- always shown, even for an info-only row."""

    info_only: bool = False
    """Nothing to fix, nothing to confirm -- e.g. auth state (surfaced,
    never changed here; /login is its own command)."""

    skip_note: str = ""
    """Set instead of ``choices`` when the check found nothing actionable
    (e.g. /migrate doesn't exist in this DOXA version) -- shown, then the
    wizard moves on with no confirmation to answer."""

    choices: "tuple[Choice, ...]" = ()
    """One or more ways to resolve this finding. A single-choice finding
    is a plain yes/skip fix; more than one is a real decision (the LORE
    plugin-store case)."""

    action: str = ""
    """A tag SetupScreen handles directly rather than through ``choices``
    -- currently only :data:`ACTION_OPEN_SETTINGS`, for the model/effort
    step, which hands off to the settings modal (the one surface that
    already edits those knobs) rather than re-implementing text entry
    here."""


# -- individual checks ------------------------------------------------


def _auth_finding() -> Finding:
    """Surfaced, never fixed here -- /login and /logout own that."""
    lines: list[str] = []
    for name in auth_mod.provider_names():
        provider = auth_mod.PROVIDERS[name]
        if not provider.installed():
            lines.append(f"{provider.label}: CLI not installed")
            continue
        authed = False
        try:
            probe = subprocess.run(
                list(provider.probe_cmd),
                capture_output=True,
                timeout=AUTH_PROBE_TIMEOUT_SECS,
            )
            authed = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            authed = False
        status = "authenticated" if authed else "not authenticated -- /login"
        lines.append(f"{provider.label}: {status}")
    return Finding(
        id="auth", title="auth state", state="\n".join(lines), info_only=True,
    )


def _lore_store_finding() -> Finding:
    env_value = os.environ.get("LORE_ROOT", "").strip()
    if env_value:
        return Finding(
            id="lore-store", title="LORE store",
            state=f"env LORE_ROOT overrides everything here: {env_value}",
            info_only=True,
        )
    stored = config_mod.load().get("lore_root")
    if stored:
        return Finding(
            id="lore-store", title="LORE store",
            state=f"already chosen by an earlier /setup: {stored}",
            info_only=True,
        )

    doxa_store = config_mod.doxa_home() / DOXA_LORE_DIR_NAME

    def _use(path: Path, note: str) -> Callable[[], str]:
        def _apply() -> str:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
            config_mod.save_lore_root(str(path))
            return f"LORE store set to {path} ({note})"
        return _apply

    if PLUGIN_LORE_DIR.is_dir():
        return Finding(
            id="lore-store", title="LORE store",
            state=(
                f"an existing store the Claude Code LORE plugin uses was "
                f"found at {PLUGIN_LORE_DIR}"
            ),
            choices=(
                Choice(
                    "use the plugin's store",
                    f"{PLUGIN_LORE_DIR} -- one memory shared between DOXA "
                    "and the plugin",
                    _use(PLUGIN_LORE_DIR, "shared with the plugin"),
                ),
                Choice(
                    "create a separate DOXA store",
                    f"{doxa_store} -- never touches the plugin's memory",
                    _use(doxa_store, "DOXA-only"),
                ),
            ),
        )

    return Finding(
        id="lore-store", title="LORE store",
        state="no LORE store found yet (no env override, nothing chosen "
              "before, no existing plugin store)",
        choices=(
            Choice(
                "create it", f"{doxa_store} -- a fresh DOXA-only store",
                _use(doxa_store, "created"),
            ),
        ),
    )


def _migrate_finding() -> Finding:
    if commands_mod.find("/migrate") is None:
        return Finding(
            id="migrate", title="/migrate", state="not offered in this DOXA version",
            skip_note="skipped -- nothing to migrate from",
        )

    def _apply() -> str:
        return "run /migrate from the prompt to continue"

    return Finding(
        id="migrate", title="/migrate", state="a /migrate command is available",
        choices=(Choice("run it next", "opens /migrate in the prompt", _apply),),
    )


def _model_effort_finding() -> Finding:
    model_source, model_value = config_mod.provenance("DOXA_MODEL")
    effort_source, effort_value = config_mod.provenance("DOXA_EFFORT")
    state = (
        f"model: {model_value or '(CLI default)'} ({model_source})\n"
        f"effort: {effort_value or '(CLI default)'} ({effort_source})"
    )
    return Finding(
        id="model-effort", title="model & effort defaults", state=state,
        choices=(
            Choice(
                "open Settings now", "Settings modal, Session tab",
                lambda: "opening Settings…",
            ),
        ),
        action=ACTION_OPEN_SETTINGS,
    )


CHECKS: "tuple[Callable[[], Finding], ...]" = (
    _auth_finding,
    _lore_store_finding,
    _migrate_finding,
    _model_effort_finding,
)


def collect_findings() -> list[Finding]:
    """Every check, in the order the wizard walks them. Blocking (the auth
    probe shells out) -- callers run it off the event loop, the same
    discipline ``doxa.naming.name_for`` documents for its own subprocess."""
    return [check() for check in CHECKS]


def doctor_placeholder() -> str:
    """``/doctor`` doesn't exist in this DOXA version yet -- this is the
    explicit placeholder line, same wording convention as
    ``scripts/install.sh``'s, wired to the real thing once it ships."""
    return "doctor: not available in this DOXA version yet"


def summary(results: "list[str]") -> str:
    lines = ["setup: done.", ""]
    lines.extend(f"  {line}" for line in results)
    lines.append("")
    lines.append(doctor_placeholder())
    return "\n".join(lines)


# -- the screen -------------------------------------------------------


class SetupScreen(ModalScreen["str | None"]):
    """Walks :func:`collect_findings` ONE finding at a time, each behind
    its own confirmation -- never a batch of silent changes. Dismisses
    with :data:`ACTION_OPEN_SETTINGS` when the model/effort step asked to
    hand off to the settings modal, ``None`` otherwise; ``doxa.app``'s
    ``action_setup`` is what acts on that."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self) -> None:
        super().__init__()
        self.findings: list[Finding] = []
        self.index = 0
        self.results: list[str] = []
        self.open_settings = False

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-panel"):
            with Horizontal(id="setup-header"):
                yield Static("▎ setup", id="setup-title")
                yield Static("✕", id="setup-close", classes="panel-close")
            yield Static("checking…", id="setup-body")
            yield Static("", id="setup-hint")

    async def on_mount(self) -> None:
        # The auth probe shells out -- off the event loop, same discipline
        # doxa.naming.name_for documents for its own subprocess call.
        self.findings = await asyncio.to_thread(collect_findings)
        self.index = 0
        self._render_step()

    def _current(self) -> "Finding | None":
        return self.findings[self.index] if self.index < len(self.findings) else None

    def _render_step(self) -> None:
        finding = self._current()
        body = self.query_one("#setup-body", Static)
        hint = self.query_one("#setup-hint", Static)
        if finding is None:
            body.update(summary(self.results))
            hint.update("enter / esc: close")
            return
        lines = [
            f"[{self.index + 1}/{len(self.findings)}] {finding.title}",
            "",
            finding.state,
        ]
        if finding.skip_note:
            lines += ["", finding.skip_note]
            hint.update("enter: continue")
        elif finding.info_only:
            hint.update("enter: continue")
        else:
            lines.append("")
            for position, choice in enumerate(finding.choices, start=1):
                lines.append(f"  {position}. {choice.label} — {choice.detail}")
            keys = "/".join(str(n) for n in range(1, len(finding.choices) + 1))
            hint.update(f"{keys}: apply · s: skip this step · esc: close")
        body.update("\n".join(lines))

    def _advance(self, result: "str | None" = None) -> None:
        if result:
            self.results.append(result)
        self.index += 1
        self._render_step()

    def action_close(self) -> None:
        self.dismiss(ACTION_OPEN_SETTINGS if self.open_settings else None)

    def on_key(self, event: events.Key) -> None:
        finding = self._current()
        if finding is None:
            if event.key in ("enter", "escape"):
                event.stop()
                self.action_close()
            return
        if event.key == "escape":
            event.stop()
            self.action_close()
            return
        if finding.skip_note or finding.info_only:
            if event.key == "enter":
                event.stop()
                note = finding.skip_note or "reviewed"
                self._advance(f"{finding.title}: {note}")
            return
        if event.key == "s":
            event.stop()
            self._advance(f"{finding.title}: skipped")
            return
        if event.key.isdigit():
            picked = int(event.key)
            if 1 <= picked <= len(finding.choices):
                event.stop()
                choice = finding.choices[picked - 1]
                message = choice.apply()
                if finding.action == ACTION_OPEN_SETTINGS:
                    self.open_settings = True
                self._advance(f"{finding.title}: {message}")

    @on(events.Click, "#setup-close")
    def _on_close_clicked(self, event: events.Click) -> None:
        event.stop()
        self.action_close()
