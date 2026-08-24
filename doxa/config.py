"""doxa.config -- the settings file, and the precedence rule around it.

One rule, everywhere: **environment > config file > default.**

An env var is a deliberate act with a narrower scope than a file (a shell,
a launcher, a systemd unit, a test), so it must beat the file it can't see.
The config file (``$DOXA_HOME/config.toml``, default ``~/.doxa/config.toml``)
is where the settings modal writes what the user picked. A default is what
DOXA does when neither says otherwise.

The knobs here are exactly the knobs that already DO something -- each row
names the code that reads it. This module does not invent settings; it
gives the existing env knobs a persistent home and one lookup function
(:func:`raw`) that every reader now calls instead of ``os.environ.get``.
That single substitution is what makes the file effective without any
consumer growing settings logic of its own.

Nothing here is a credential store: the settings are model names, seconds,
thresholds and display toggles. Values are written back as TOML by a
deliberately small writer (str/float/int/bool only) rather than a
dependency, because the file has to stay hand-editable and boring.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Setting:
    """One knob: where it is stored, what overrides it, what reads it."""

    key: str
    """Key in config.toml. Empty for a read-only, display-only row."""

    env: str
    """Environment variable that overrides the file."""

    label: str
    """Row label in the settings modal."""

    help: str
    """One line: what it does, and what reads it."""

    kind: str = "str"
    """str | number | bool | bool_on | choice | strftime -- drives
    validation, not widgets. ``bool_on`` is ``bool`` for a knob that
    defaults ON: see the note on :func:`_coerce` for why it needs a
    different STORAGE representation, not just a different default."""

    choices: tuple[str, ...] = ()
    default: str = ""
    read_only: bool = False

    category: str = "Session"
    """Which tab of the settings modal this row lives on."""

    note: str = ""
    """Extra line under the help, for rows that need a caveat."""

    def placeholder(self) -> str:
        if self.choices:
            return " | ".join(c for c in self.choices if c)
        if self.kind in ("bool", "bool_on"):
            return "1 = on, empty = off" if self.kind == "bool" else "1 = on, 0 = off (empty = on)"
        if self.kind == "strftime":
            return "e.g. %a %H:%M (empty = built-in format)"
        return self.default or "(default)"


# The knobs, in the order the modal shows them. Every row is load-bearing:
# there are no placeholder settings here, because a settings menu that
# lists something inert teaches the user that the menu lies.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="model", env="DOXA_MODEL", label="model", category="Session",
        help="Model for new sessions (doxa.cli --model default; /model "
             "switches the live session)",
    ),
    Setting(
        key="effort", env="DOXA_EFFORT", label="effort", category="Session",
        kind="choice", choices=("", "low", "medium", "high", "xhigh", "max"),
        help="Effort level for NEW sessions (doxa.engine.effort_level)",
        note="ClaudeAgentOptions.effort is a connect-time option -- the SDK "
             "has no live setter, so a running session keeps its own.",
    ),
    Setting(
        key="linger_secs", env="DOXA_LINGER_SECS", label="linger secs",
        category="Session", kind="number", default="120",
        help="Seconds a daemon outlives its last client before finalizing "
             "(doxa.cli --linger default)",
    ),
    Setting(
        key="worktree_per_session", env="DOXA_WORKTREE",
        label="worktree per session", category="Session",
        kind="bool_on", default="1",
        help="Give each session its own git worktree (isolated edits, own "
             "branch doxa/<id>) instead of sharing the launch directory "
             "(doxa.worktrees.create)",
        note="Off returns to today's behavior exactly: every session runs "
             "directly in the launch directory. A clean, unmerged worktree "
             "is removed with its branch when the session ends; anything "
             "committed or dirty is kept for you to merge by hand -- never "
             "auto-merged.",
    ),
    Setting(
        key="derive_secs", env="DOXA_DERIVE_SECS", label="derive secs",
        category="Memory", kind="number",
        help="Streaming-deriver debounce interval, seconds; empty = off "
             "(doxa.engine.derive_interval)",
    ),
    Setting(
        key="consult_floor", env="DOXA_CONSULT_FLOOR", label="consult floor",
        category="Memory", kind="number", default="1.0",
        help="bm25 relevance floor for the act-time belief consult; 0 "
             "disables it (doxa.engine.consult_floor)",
    ),
    Setting(
        key="lore_root", env="LORE_ROOT", label="lore store", category="Memory",
        help="Where the belief store and session index live (lore_core.ROOT)",
        note="Shared with the Claude Code LORE plugin -- one store, two "
             "carriers. Set LORE_ROOT to point elsewhere; a private store "
             "would fork your memory into two divergent halves. /setup "
             "makes and stickies this choice -- read_only here because "
             "this row is /setup's, not the settings modal's, to edit.",
        read_only=True,
    ),
    Setting(
        key="nerd_font", env="DOXA_NERD_FONT", label="nerd font",
        category="Appearance", kind="bool",
        help="Use the nerd-font branch glyph instead of the branch sign in "
             "the status line (doxa.app.git_branch_symbol)",
    ),
    Setting(
        key="image_mode", env="DOXA_IMAGE_MODE", label="image mode",
        category="Appearance", kind="choice",
        choices=("", "kgp", "sixel", "halfblock", "text"),
        help="Force a rung of the terminal-image ladder; empty = probe "
             "(doxa.images.detect_mode)",
    ),
    Setting(
        key="clock_show", env="DOXA_CLOCK_SHOW", label="clock: show",
        category="Appearance", kind="bool_on", default="1",
        help="Show the fixed-width clock at the right edge of the tab "
             "bar (doxa.clock.ClockConfig)",
        note="The one bool setting in this app that defaults ON -- an "
             "empty field here still means the clock shows; type 0 to "
             "turn it off.",
    ),
    Setting(
        key="clock_date", env="DOXA_CLOCK_DATE", label="clock: show date",
        category="Appearance", kind="bool",
        help="Prefix the clock with %Y-%m-%d (doxa.clock.builtin_format)",
    ),
    Setting(
        key="clock_hour", env="DOXA_CLOCK_HOUR", label="clock: hour format",
        category="Appearance", kind="choice", choices=("", "12", "24"),
        default="24",
        help="12- or 24-hour clock (doxa.clock.builtin_format)",
    ),
    Setting(
        key="clock_seconds", env="DOXA_CLOCK_SECONDS",
        label="clock: show seconds", category="Appearance", kind="bool",
        help="Show :SS; also switches the clock's one timer from minute- "
             "to second-aligned (doxa.clock.seconds_until_boundary)",
    ),
    Setting(
        key="clock_tz", env="DOXA_CLOCK_TZ", label="clock: timezone",
        category="Appearance",
        help="IANA zone name, e.g. Europe/Berlin; empty = system local "
             "(doxa.clock.resolve_tz)",
        note="An unresolvable name falls back to system local time, "
             "visibly (the clock's tooltip says so) rather than silently.",
    ),
    Setting(
        key="clock_format", env="DOXA_CLOCK_FORMAT",
        label="clock: custom format", category="Appearance",
        kind="strftime",
        help="strftime format overriding the toggles above "
             "(doxa.clock.render)",
        note="Validated on save (a value strftime rejects is not stored); "
             "a value that becomes invalid later (a hand-edited file, an "
             "env var) falls back to the built-in format at render time, "
             "visibly, rather than crashing the clock.",
    ),
    Setting(
        key="notify", env="DOXA_NOTIFY", label="notify",
        category="Notifications", kind="choice",
        choices=("auto", "always", "off"), default="auto",
        help="When to send desktop notifications: auto (only while the "
             "terminal window is unfocused), always, or off (doxa.notify)",
    ),
    Setting(
        key="notify_turn_done", env="DOXA_NOTIFY_TURN_DONE",
        label="notify: turn done", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when a turn finishes (doxa.notify.notify_turn_done)",
    ),
    Setting(
        key="notify_update", env="DOXA_NOTIFY_UPDATE",
        label="notify: update available", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when /update has something to pull "
             "(doxa.notify.notify_update_available)",
    ),
    Setting(
        key="notify_lore", env="DOXA_NOTIFY_LORE",
        label="notify: lore review", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when LORE stages memory proposals; off also silences "
             "lore_core's own in-process notification (LORE_NOTIFY) -- see "
             "doxa.notify.sync_lore_notify_env",
    ),
    Setting(
        key="notify_needs_input", env="DOXA_NOTIFY_NEEDS_INPUT",
        label="notify: needs input", category="Notifications",
        kind="bool_on", default="1",
        help="Notify when a session is waiting on you",
        note="Reserved: the trigger (can_use_tool / AskUserQuestion "
             "plumbing) is phase 2 -- this row exists but nothing fires it "
             "yet.",
    ),
    Setting(
        key="", env="DOXA_HOME", label="doxa home", category="Paths",
        help="Durable DOXA state: this config, the window layout",
        read_only=True,
    ),
    Setting(
        key="", env="DOXA_RUNTIME_DIR", label="runtime dir", category="Paths",
        help="Ephemeral endpoints: daemon sockets and the peer registry "
             "(doxa.peers.runtime_dir)",
        note="Deliberately NOT under ~/.doxa: home directories can be NFS "
             "(AF_UNIX misbehaves) and stale sockets must not outlive a "
             "reboot.",
        read_only=True,
    ),
)

SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS if s.key}
SETTINGS_BY_ENV: dict[str, Setting] = {s.env: s for s in SETTINGS}


# Where DOXA's durable state lives. Deliberately ~/.doxa (DOXA_HOME
# overrides), mirroring ~/.claude -- and deliberately NOT the runtime dir:
#
#   ~/.doxa        durable state: config.toml, window layout, anything that
#                  must survive a reboot.
#   runtime dir    ephemeral endpoints: the daemon sockets and the peer
#                  registry ($DOXA_RUNTIME_DIR -> $XDG_RUNTIME_DIR/doxa ->
#                  ~/.local/share/doxa). Sockets stay there because a home
#                  directory can be NFS (AF_UNIX misbehaves there) and
#                  because stale socket files must not survive a reboot,
#                  which the runtime dir's tmpfs semantics guarantee.
#
# The LORE store is neither: it stays lore_core's own (~/.claude/lore,
# LORE_ROOT-overridable) because sharing one store with the Claude Code
# plugin is a product property -- a private DOXA store would silently fork
# the user's memory and beliefs into two divergent halves.
_MIGRATED = False


def doxa_home() -> Path:
    base = os.environ.get("DOXA_HOME", "").strip()
    return Path(base) if base else Path.home() / ".doxa"


def config_dir() -> Path:
    return doxa_home()


def config_path() -> Path:
    return doxa_home() / "config.toml"


def legacy_config_path() -> Path:
    """Where an early build wrote it (XDG). Migrated once, then forgotten."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "doxa" / "config.toml"


def migrate_legacy() -> "Path | None":
    """Move a pre-~/.doxa config into place, once per process. Returns the
    destination when something was actually moved, else None -- so the
    caller can say so out loud rather than silently relocating a file."""
    global _MIGRATED
    if _MIGRATED:
        return None
    _MIGRATED = True
    destination = config_path()
    legacy = legacy_config_path()
    if destination.exists() or not legacy.exists() or legacy == destination:
        return None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        os.replace(legacy, destination)
    except OSError:
        return None
    invalidate()
    return destination


# Cache key: (path, mtime, size) -- same discipline as doxa.identity. The
# file is read on every knob lookup (per turn, per status refresh), so
# re-parsing it each time would be a small tax on a hot path. A save moves
# the mtime; save() also invalidates directly for same-tick rewrites.
_CACHE: "tuple[tuple[str, float, int], dict[str, Any]] | None" = None


def invalidate() -> None:
    global _CACHE
    _CACHE = None


def load() -> dict[str, Any]:
    """The config file as a flat dict, or ``{}``.

    Never raises: a missing file, an unreadable one and malformed TOML all
    mean "no stored settings" to every caller -- a broken config must cost
    the user their customizations, never their session."""
    global _CACHE
    path = config_path()
    try:
        stat = path.stat()
    except OSError:
        _CACHE = None
        return {}
    key = (str(path), stat.st_mtime, stat.st_size)
    if _CACHE is not None and _CACHE[0] == key:
        return _CACHE[1]
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _CACHE = (key, data)
    return data


def raw(env_name: str) -> str:
    """The effective value of one knob, as a string: env, then file, then
    "" (which every existing reader already treats as "use the default").

    This is the single substitution that makes the config file real -- the
    readers in engine/app/images call THIS instead of os.environ.get, and
    keep their own parsing and their own defaults."""
    value = os.environ.get(env_name, "")
    if value.strip():
        return value
    setting = SETTINGS_BY_ENV.get(env_name)
    if setting is None or not setting.key:
        return ""
    stored = load().get(setting.key)
    if stored is None or stored == "":
        return ""
    if isinstance(stored, bool):
        return "1" if stored else ""
    return str(stored)


def effective(env_name: str) -> str:
    """:func:`raw` with the declared default filled in -- for display."""
    value = raw(env_name)
    if value.strip():
        return value
    setting = SETTINGS_BY_ENV.get(env_name)
    return setting.default if setting else ""


def provenance(env_name: str) -> tuple[str, str]:
    """``(source, value)`` for one knob -- where the effective value came
    from, resolved through the same precedence every reader uses.

    ``source`` is "env", "config" or "default". The settings modal shows
    this next to every row: a value the user cannot change from the UI must
    be visibly EXPLAINED, not mysteriously ignored."""
    env_value = os.environ.get(env_name, "")
    if env_value.strip():
        return "env", env_value
    setting = SETTINGS_BY_ENV.get(env_name)
    if setting is not None and setting.key:
        stored = load().get(setting.key)
        if stored is not None and stored != "":
            if isinstance(stored, bool):
                return "config", "true" if stored else "false"
            return "config", str(stored)
    return "default", (setting.default if setting else "")


def source_label(env_name: str) -> str:
    """The human form of :func:`provenance`'s source, naming the env var
    that is winning when one is."""
    source, _value = provenance(env_name)
    if source == "env":
        return f"env {env_name} — overrides config"
    return source


def overridden_by_env(env_name: str) -> bool:
    """True when the environment is what is winning -- the settings modal
    says so out loud, because an edit that silently does nothing is the
    worst thing a settings menu can do."""
    return bool(os.environ.get(env_name, "").strip())


def linger_secs() -> float:
    """The daemon linger knob, parsed. Garbage falls back to the default
    rather than crashing the CLI on a typo in a config file."""
    from .daemon import DEFAULT_LINGER_SECS

    value = raw("DOXA_LINGER_SECS").strip()
    if not value:
        return DEFAULT_LINGER_SECS
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_LINGER_SECS
    return parsed if parsed >= 0 else DEFAULT_LINGER_SECS


def model() -> "str | None":
    """The configured model for new sessions, or None for the CLI default."""
    value = raw("DOXA_MODEL").strip()
    return value or None


# -- writing ---------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _coerce(setting: Setting, value: str) -> "Any | None":
    """String from the modal -> the value stored in TOML. None means "drop
    this key" (an emptied field returns the knob to its default), which is
    how the modal expresses "unset" without a third state."""
    value = (value or "").strip()
    if not value:
        return None
    if setting.kind == "number":
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    if setting.kind in ("bool", "bool_on"):
        truthy = value.lower() not in ("0", "false", "no", "off")
        if setting.kind == "bool_on":
            # Stored as the STRING "0"/"1", never a Python bool. raw()
            # collapses an actual bool False to "" (so a bool row's
            # "unset" and "explicitly off" read the same) -- harmless for
            # every OTHER bool knob, whose default is off already, but it
            # would make an explicit "off" on a DEFAULT-ON knob (clock_show
            # is the one so far) indistinguishable from never having
            # touched it. A string survives raw() as itself.
            return "1" if truthy else "0"
        return truthy
    if setting.kind == "strftime":
        # Reject at save time what the render path would otherwise have
        # to fall back from silently -- doxa.clock.render carries the
        # same try/except as a second line of defense, for a value that
        # becomes invalid AFTER being saved (a hand-edited file, an env
        # var on a different platform's libc).
        import datetime as _dt

        try:
            text = _dt.datetime.now().strftime(value)
        except (ValueError, TypeError):
            return None
        if not text.strip():
            return None
        return value
    if setting.choices and value not in setting.choices:
        return None
    return value


def _write_stored(stored: dict[str, Any]) -> Path:
    """The shared tail of every writer: render ``stored`` as TOML and
    replace the file atomically, clamped to 0600 -- it is user
    configuration, not something a shared machine reads."""
    lines = [
        "# DOXA settings. Precedence: environment > this file > default.",
        "# Written by the settings modal (Ctrl+, or /settings); safe to edit.",
        "",
    ]
    for setting in SETTINGS:
        if setting.key and setting.key in stored:
            lines.append(f"{setting.key} = {_toml_value(stored[setting.key])}")
    # Keys DOXA no longer knows about are preserved verbatim rather than
    # dropped: a config written by a newer version must survive an older one.
    for key in sorted(k for k in stored if k not in SETTINGS_BY_KEY):
        lines.append(f"{key} = {_toml_value(stored[key])}")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # DOXA's state home is the user's alone
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    invalidate()
    return path


def save(values: dict[str, str]) -> Path:
    """Write the settings file from ``{key: string}`` (the modal's fields).

    Keys absent from ``values`` keep whatever the file already had; keys
    present but empty are REMOVED, which is what returns a knob to its
    default. Read-only rows are skipped even if present in ``values`` --
    the modal must never be the thing that writes a row it renders with no
    field (see :func:`save_lore_root` for the one row that DOES get
    written outside the modal).
    """
    stored = dict(load())
    for setting in SETTINGS:
        if not setting.key or setting.read_only or setting.key not in values:
            continue
        coerced = _coerce(setting, values[setting.key])
        if coerced is None:
            stored.pop(setting.key, None)
        else:
            stored[setting.key] = coerced
    return _write_stored(stored)


def save_lore_root(path: str) -> Path:
    """The one write ``/setup`` makes directly: the sticky LORE store
    choice (``doxa.setup``'s ladder). Deliberately bypasses :func:`save`'s
    read-only gate on the ``lore_root`` row -- that gate exists to keep
    this row OUT of the settings modal's editable fields (it is /setup's
    to decide, once, not a field to fat-finger), not to make it
    unwritable altogether."""
    stored = dict(load())
    stored["lore_root"] = path
    return _write_stored(stored)
