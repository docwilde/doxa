# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.clock -- config precedence, format building, boundary math, and the
visible-error fallback for a bad timezone or a bad custom format.

Pure-function coverage lives here; the ONE-timer behavior of the widget
that consumes this module (doxa.app.ClockChip) is covered in
tests/test_chrome.py, next to the guard tests it has to coexist with.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from doxa import clock, config


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    for env in (
        "DOXA_CLOCK_SHOW", "DOXA_CLOCK_DATE", "DOXA_CLOCK_HOUR",
        "DOXA_CLOCK_SECONDS", "DOXA_CLOCK_TZ", "DOXA_CLOCK_FORMAT",
    ):
        monkeypatch.delenv(env, raising=False)
    config.invalidate()
    yield
    config.invalidate()


# A fixed instant, well clear of any DST transition: 2026-08-24 14:32:07 UTC.
NOW = datetime(2026, 8, 24, 14, 32, 7, 250_000, tzinfo=timezone.utc)


# -- ClockConfig.load: the one bool that defaults ON -----------------------


def test_default_config_is_on_24h_no_seconds_no_date_system_tz():
    cfg = clock.ClockConfig.load()
    assert cfg == clock.ClockConfig(
        show=True, show_date=False, hour24=True, show_seconds=False,
        tz_name="", custom_format="",
    )


def test_clock_show_is_the_one_bool_setting_that_defaults_on(monkeypatch):
    """Every other bool setting in doxa.config reads "" as off; this is
    the one that has to read "" as on, which is why it does not go
    through doxa.config.raw().strip() truthiness like git_branch_symbol
    does -- see doxa.clock._bool."""
    monkeypatch.setenv("DOXA_CLOCK_SHOW", "0")
    assert clock.ClockConfig.load().show is False
    monkeypatch.setenv("DOXA_CLOCK_SHOW", "off")
    assert clock.ClockConfig.load().show is False
    monkeypatch.delenv("DOXA_CLOCK_SHOW")
    assert clock.ClockConfig.load().show is True


def test_config_file_and_env_both_reach_the_clock(monkeypatch):
    config.save({
        "clock_date": "1", "clock_hour": "12", "clock_seconds": "1",
        "clock_tz": "Europe/Berlin", "clock_format": "%A",
    })
    cfg = clock.ClockConfig.load()
    assert cfg.show_date is True
    assert cfg.hour24 is False
    assert cfg.show_seconds is True
    assert cfg.tz_name == "Europe/Berlin"
    assert cfg.custom_format == "%A"
    monkeypatch.setenv("DOXA_CLOCK_HOUR", "24")  # env beats the file
    assert clock.ClockConfig.load().hour24 is True


# -- builtin_format ----------------------------------------------------


@pytest.mark.parametrize(
    "hour24,show_seconds,show_date,expected",
    [
        (True, False, False, "%H:%M"),
        (True, True, False, "%H:%M:%S"),
        (False, False, False, "%I:%M %p"),
        (False, True, False, "%I:%M:%S %p"),
        (True, False, True, "%Y-%m-%d %H:%M"),
    ],
)
def test_builtin_format_combinations(hour24, show_seconds, show_date, expected):
    cfg = clock.ClockConfig(
        show=True, show_date=show_date, hour24=hour24,
        show_seconds=show_seconds, tz_name="", custom_format="",
    )
    assert clock.builtin_format(cfg) == expected


# -- render: the happy paths --------------------------------------------


def test_render_24h_no_seconds():
    cfg = clock.ClockConfig(True, False, True, False, "", "")
    text, warning = clock.render(NOW, cfg)
    assert text == NOW.astimezone().strftime("%H:%M")  # no tz set: system local
    assert warning is None


def test_render_12h_with_seconds_and_date():
    cfg = clock.ClockConfig(True, True, False, True, "", "")
    text, warning = clock.render(NOW, cfg)
    assert text == NOW.astimezone().strftime("%Y-%m-%d %I:%M:%S %p")
    assert warning is None


def test_render_honors_a_valid_timezone():
    cfg = clock.ClockConfig(True, False, True, False, "America/New_York", "")
    text, warning = clock.render(NOW, cfg)
    local = NOW.astimezone(ZoneInfo("America/New_York"))
    assert text == local.strftime("%H:%M")
    assert warning is None


def test_render_honors_a_valid_custom_format():
    cfg = clock.ClockConfig(True, False, True, False, "", "%a %H:%M")
    text, warning = clock.render(NOW, cfg)
    assert text == NOW.astimezone().strftime("%a %H:%M")
    assert warning is None


# -- render: the visible-error fallback ----------------------------------


def test_render_falls_back_on_an_unresolvable_timezone():
    cfg = clock.ClockConfig(True, False, True, False, "Not/A_Real_Zone", "")
    text, warning = clock.render(NOW, cfg)
    assert text == NOW.astimezone().strftime("%H:%M")  # falls back to system local
    assert warning is not None
    assert "Not/A_Real_Zone" in warning


def test_render_falls_back_on_a_custom_format_strftime_rejects():
    # A format that strftime silently reduces to an EMPTY string on this
    # platform (glibc's strftime is permissive: bad directives are rarely
    # a raised ValueError) -- render()'s empty-result guard is what turns
    # that into a fallback rather than a blank clock.
    cfg = clock.ClockConfig(True, False, True, False, "", "%9999999999f")
    text, warning = clock.render(NOW, cfg)
    assert text == NOW.astimezone().strftime("%H:%M")
    assert warning is not None
    assert "9999999999" in warning


def test_render_warning_carries_both_failures_at_once():
    cfg = clock.ClockConfig(
        True, False, True, False, "Not/A_Real_Zone", "%9999999999f",
    )
    _text, warning = clock.render(NOW, cfg)
    assert "Not/A_Real_Zone" in warning
    assert "9999999999" in warning


def test_render_never_raises_on_a_hostile_custom_format():
    """Whatever a user pastes into the field, the clock must not crash --
    that is the whole point of the fallback existing."""
    for bad in ("%", "%q%z%Q", "\x00%H", "%" * 50):
        cfg = clock.ClockConfig(True, False, True, False, "", bad)
        text, _warning = clock.render(NOW, cfg)
        assert text  # degraded, never empty, never an exception


# -- resolve_tz ------------------------------------------------------------


def test_resolve_tz_empty_means_system_local_no_error():
    assert clock.resolve_tz("") == (None, None)


def test_resolve_tz_valid_name():
    tz, error = clock.resolve_tz("UTC")
    assert tz is not None
    assert error is None


def test_resolve_tz_invalid_name_reports_and_falls_back():
    tz, error = clock.resolve_tz("Definitely/Not/A/Zone")
    assert tz is None
    assert error is not None


# -- seconds_until_boundary -----------------------------------------------


def test_minute_aligned_delay():
    now = datetime(2026, 8, 24, 14, 32, 45, 250_000, tzinfo=timezone.utc)
    delay = clock.seconds_until_boundary(now, show_seconds=False)
    assert delay == pytest.approx(60 - 45 - 0.25, abs=1e-6)


def test_second_aligned_delay():
    now = datetime(2026, 8, 24, 14, 32, 45, 250_000, tzinfo=timezone.utc)
    delay = clock.seconds_until_boundary(now, show_seconds=True)
    assert delay == pytest.approx(1 - 0.25, abs=1e-6)


def test_delay_is_always_positive_even_exactly_on_a_boundary():
    now = datetime(2026, 8, 24, 14, 33, 0, 0, tzinfo=timezone.utc)
    assert clock.seconds_until_boundary(now, show_seconds=False) > 0
    assert clock.seconds_until_boundary(now, show_seconds=True) > 0


# -- config plumbing: the strftime kind validates on save -------------------


def test_saving_a_bad_custom_format_is_rejected_not_stored():
    config.save({"clock_format": "%9999999999f"})  # strftime -> "" on this platform
    assert "clock_format" not in config.load()


def test_saving_a_good_custom_format_is_stored():
    config.save({"clock_format": "%a %H:%M"})
    assert config.load()["clock_format"] == "%a %H:%M"
