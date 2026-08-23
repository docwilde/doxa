"""doxa.config -- the settings file, and the precedence rule around it.

One rule, everywhere: **environment > config file > default.**

An env var is a deliberate act with a narrower scope than a file (a shell,
a launcher, a systemd unit, a test), so it must beat the file it can't see.
The config file (XDG: ``$XDG_CONFIG_HOME/doxa/config.toml``, else
``~/.config/doxa/config.toml``) is where the settings modal writes what the
user picked. A default is what DOXA does when neither says otherwise.

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
    """str | number | bool | choice -- drives validation, not widgets."""

    choices: tuple[str, ...] = ()
    default: str = ""
    read_only: bool = False

    def placeholder(self) -> str:
        if self.choices:
            return " | ".join(c for c in self.choices if c)
        if self.kind == "bool":
            return "1 = on, empty = off"
        return self.default or "(default)"


# The knobs, in the order the modal shows them. Every row is load-bearing:
# there are no placeholder settings here, because a settings menu that
# lists something inert teaches the user that the menu lies.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="model", env="DOXA_MODEL", label="model",
        help="Model for new sessions (doxa.cli --model default; /model "
             "switches the live session)",
    ),
    Setting(
        key="effort", env="DOXA_EFFORT", label="effort",
        kind="choice", choices=("", "low", "medium", "high", "xhigh", "max"),
        help="Effort level for NEW sessions; the SDK sets it at connect only, "
             "so a running session keeps its own (doxa.engine.effort_level)",
    ),
    Setting(
        key="derive_secs", env="DOXA_DERIVE_SECS", label="derive secs",
        kind="number",
        help="Streaming-deriver debounce interval, seconds; empty = off "
             "(doxa.engine.derive_interval)",
    ),
    Setting(
        key="linger_secs", env="DOXA_LINGER_SECS", label="linger secs",
        kind="number", default="120",
        help="Seconds a daemon outlives its last client before finalizing "
             "(doxa.cli --linger default)",
    ),
    Setting(
        key="consult_floor", env="DOXA_CONSULT_FLOOR", label="consult floor",
        kind="number", default="1.0",
        help="bm25 relevance floor for the act-time belief consult; 0 "
             "disables it (doxa.engine.consult_floor)",
    ),
    Setting(
        key="nerd_font", env="DOXA_NERD_FONT", label="nerd font",
        kind="bool",
        help="Use the nerd-font branch glyph  instead of ⎇ in the status "
             "line (doxa.app.git_branch_symbol)",
    ),
    Setting(
        key="image_mode", env="DOXA_IMAGE_MODE", label="image mode",
        kind="choice", choices=("", "kgp", "sixel", "halfblock", "text"),
        help="Force a rung of the terminal-image ladder; empty = probe "
             "(doxa.images.detect_mode)",
    ),
    Setting(
        key="", env="LORE_ROOT", label="lore store",
        help="Where the belief store and session index live (lore_core.ROOT)",
        read_only=True,
    ),
)

SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS if s.key}
SETTINGS_BY_ENV: dict[str, Setting] = {s.env: s for s in SETTINGS}


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".config") / "doxa"


def config_path() -> Path:
    return config_dir() / "config.toml"


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
    if setting.kind == "bool":
        return value.lower() not in ("0", "false", "no", "off")
    if setting.choices and value not in setting.choices:
        return None
    return value


def save(values: dict[str, str]) -> Path:
    """Write the settings file from ``{key: string}`` (the modal's fields).

    Keys absent from ``values`` keep whatever the file already had; keys
    present but empty are REMOVED, which is what returns a knob to its
    default. The file is written atomically (tmp + replace) and clamped to
    0600 -- it is user configuration, not something a shared machine reads.
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
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    invalidate()
    return path
