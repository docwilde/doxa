"""doxa.clock -- the upper-right clock: settings, formatting, boundary math.

Pure functions only; the widget that owns the ONE timer lives in
``doxa.app.ClockChip``. Splitting it this way is what lets the format/
boundary/timezone logic be tested without a running Textual app, and lets
``scripts/screenshot.py`` freeze "now" for a deterministic gallery scene by
monkeypatching :func:`now_utc` alone.

Six knobs, all in ``config.SETTINGS`` (category "Appearance", same tab as
``nerd_font``/``image_mode``):

============  ===================  =======  ============================
key           env                  default  what
============  ===================  =======  ============================
clock_show    DOXA_CLOCK_SHOW      on       show the clock at all
clock_date    DOXA_CLOCK_DATE      off      prefix ``%Y-%m-%d``
clock_hour    DOXA_CLOCK_HOUR      24       ``12`` or ``24``
clock_seconds DOXA_CLOCK_SECONDS   off      show ``:SS`` (also what makes
                                             the timer second- rather than
                                             minute-aligned)
clock_tz      DOXA_CLOCK_TZ        system   IANA name, e.g. Europe/Berlin
clock_format  DOXA_CLOCK_FORMAT    (none)   strftime, overrides the above
============  ===================  =======  ============================

``clock_show`` is the one knob here that defaults ON rather than off --
:func:`_bool` is why ``config.raw``'s "empty means off" convention (right
for every other bool setting in this app) does not silently turn the clock
off for everyone who has never touched ~/.doxa/config.toml.

Never raises: an unresolvable timezone name or a custom format that
``strftime`` chokes on both degrade to a safe default (system-local time,
the built-in format) rather than taking the one widget in the corner down
-- and the degradation is VISIBLE, not silent, via the warning string
:func:`render` returns (the widget surfaces it as a tooltip).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config as config_mod


def _bool(env_name: str, default: bool) -> bool:
    """Same truthy/falsy vocabulary as ``config._coerce``'s bool kind, but
    able to default ON -- ``config.raw`` returns "" for both "never set"
    and "explicitly off", and only the caller knows which default that
    silence should mean."""
    raw = config_mod.raw(env_name).strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class ClockConfig:
    show: bool
    show_date: bool
    hour24: bool
    show_seconds: bool
    tz_name: str
    custom_format: str

    @classmethod
    def load(cls) -> "ClockConfig":
        """The effective config, read fresh through ``doxa.config``'s
        precedence (env > file > default) -- called at mount and whenever
        the settings modal saves, never cached across a turn."""
        hour = config_mod.raw("DOXA_CLOCK_HOUR").strip() or "24"
        return cls(
            show=_bool("DOXA_CLOCK_SHOW", True),
            show_date=_bool("DOXA_CLOCK_DATE", False),
            hour24=hour != "12",
            show_seconds=_bool("DOXA_CLOCK_SECONDS", False),
            tz_name=config_mod.raw("DOXA_CLOCK_TZ").strip(),
            custom_format=config_mod.raw("DOXA_CLOCK_FORMAT").strip(),
        )


def resolve_tz(name: str) -> "tuple[tzinfo | None, str | None]":
    """``(tzinfo, error)``. Empty name -> ``(None, None)``, which
    ``datetime.astimezone(None)`` treats as "system local" -- the same
    thing an unresolvable name falls back to, except THAT case also
    returns a warning the caller must surface."""
    if not name:
        return None, None
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None, f"unknown timezone {name!r} — showing system local time"


def builtin_format(cfg: ClockConfig) -> str:
    """The format built from the individual toggles -- what shows when
    there is no custom format, and what a bad custom format falls back
    to."""
    if cfg.show_seconds:
        time_fmt = "%H:%M:%S" if cfg.hour24 else "%I:%M:%S %p"
    else:
        time_fmt = "%H:%M" if cfg.hour24 else "%I:%M %p"
    return f"%Y-%m-%d {time_fmt}" if cfg.show_date else time_fmt


def render(now: datetime, cfg: ClockConfig) -> "tuple[str, str | None]":
    """The text to show, and a warning to surface (the widget's tooltip)
    when a stored value could not be honored as-is. Two independent
    failure modes can each contribute a clause: an unresolvable timezone
    name, and a custom format ``strftime`` rejects (or that renders to
    nothing at all, which is what an all-format-code-stripping platform
    does with a genuinely empty result)."""
    tz, tz_error = resolve_tz(cfg.tz_name)
    local = now.astimezone(tz)
    fmt = cfg.custom_format or builtin_format(cfg)
    fmt_error = None
    try:
        text = local.strftime(fmt)
        if not text.strip():
            raise ValueError("custom clock format produced no text")
    except (ValueError, TypeError) as exc:
        fmt_error = f"invalid custom format {cfg.custom_format!r} ({exc}) — showing the default"
        text = local.strftime(builtin_format(cfg))
    warning = " · ".join(w for w in (tz_error, fmt_error) if w) or None
    return text, warning


def seconds_until_boundary(now: datetime, show_seconds: bool) -> float:
    """Seconds until the next tick edge: the next SECOND when seconds are
    shown, the next MINUTE when they are not -- never the fixed period a
    plain interval timer would use, which is what keeps a hidden-seconds
    clock from redrawing an identical string every second for nothing.

    Always strictly positive (floored at 50ms) so a boundary landed on
    exactly cannot re-fire the same instant twice."""
    micro = now.microsecond / 1_000_000
    if show_seconds:
        delay = 1.0 - micro
    else:
        delay = 60.0 - now.second - micro
    return max(0.05, delay)


def now_utc() -> datetime:
    """The single time source every clock read goes through -- so a test
    or the screenshot gallery can freeze "now" by monkeypatching this one
    function instead of reaching into the standard library."""
    return datetime.now(timezone.utc)
