"""doxa.doctor -- ``/doctor`` and ``doxa doctor``: read-only health checks.

READ-ONLY, deliberately: every check here observes and reports, none of
them write anything, reap anything, or fix anything -- that is what
``/setup`` is for. A check is one :class:`Check`: pass, fail, or (for the
one thing genuinely not measurable yet -- keyboard-enhancement grant, see
below) unknown, plus the EXACT fix command when it fails. :func:`run_checks`
is the one entry point both surfaces call:

* ``doxa doctor`` (``doxa/cli.py``) -- no TUI, for scripts (this is what
  ``scripts/install.sh`` runs at the end of a fresh install) and for a
  quick terminal check. Exits 1 if anything failed.
* ``/doctor`` (Tools & config) -- the same report as a SystemBlock, run
  off the event loop (``doxa.app``'s handler) because the claude-CLI auth
  probe shells out.

Keyboard enhancement is reported :data:`STATUS_UNKNOWN`, not pass/fail:
Textual's Linux driver requests the Kitty/CSI-u protocol unconditionally
at startup (``\\x1b[>1u``, see ``textual/drivers/linux_driver.py``) but
does not itself expose whether the terminal actually granted it -- DETECTING
the grant is item O's job (customizable keybindings), not this one's, and a
doctor check that guesses pass/fail for something it cannot actually
measure is worse than one that says so.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from typing import Callable

from . import auth as auth_mod
from . import config as config_mod
from . import peers as peers_mod
from . import version as version_mod

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

STATUS_GLYPH = {STATUS_PASS: "✓", STATUS_FAIL: "✗", STATUS_UNKNOWN: "?"}

CLAUDE_PROBE_TIMEOUT_SECS = 10.0
_MIN_PYTHON_RE = re.compile(r">=\s*([0-9]+\.[0-9]+)")
_DEFAULT_MIN_PYTHON = "3.11"


@dataclass(frozen=True)
class Check:
    """One health check: what's true, and -- only when it failed -- the
    exact command to fix it."""

    id: str
    title: str
    status: str
    detail: str
    fix: str = ""


# -- individual checks -------------------------------------------------


def _min_python() -> str:
    """Read live from THIS checkout's pyproject.toml -- same discipline
    scripts/install.sh uses for a remote one, so a future version raising
    the floor never needs a second place updated."""
    root = version_mod.source_root()
    if root is None:
        return _DEFAULT_MIN_PYTHON
    try:
        with (root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return _DEFAULT_MIN_PYTHON
    requires = str((data.get("project") or {}).get("requires-python") or "")
    match = _MIN_PYTHON_RE.search(requires)
    return match.group(1) if match else _DEFAULT_MIN_PYTHON


def _python_check() -> Check:
    minimum = _min_python()
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    ok = (sys.version_info.major, sys.version_info.minor) >= tuple(
        int(part) for part in minimum.split(".")
    )
    return Check(
        id="python", title="python version",
        status=STATUS_PASS if ok else STATUS_FAIL,
        detail=f"{current} running, doxa needs {minimum}+",
        fix="" if ok else f"install python {minimum}+ (e.g. `uv python install {minimum}`)",
    )


def _doxa_version_check() -> Check:
    return Check(
        id="doxa-version", title="DOXA version", status=STATUS_PASS,
        detail=version_mod.version_line(),
    )


def _claude_cli_check() -> Check:
    provider = auth_mod.PROVIDERS.get("claude")
    if provider is None or not provider.installed():
        return Check(
            id="claude-cli", title="claude CLI", status=STATUS_FAIL,
            detail="not found on PATH",
            fix="install it -- https://docs.claude.com/en/docs/claude-code",
        )
    cli_version = "version unknown"
    try:
        probe = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True,
            timeout=CLAUDE_PROBE_TIMEOUT_SECS,
        )
        text = (probe.stdout or probe.stderr).strip()
        if text:
            cli_version = text.splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    authed = False
    try:
        auth_probe = subprocess.run(
            list(provider.probe_cmd), capture_output=True,
            timeout=CLAUDE_PROBE_TIMEOUT_SECS,
        )
        authed = auth_probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        authed = False
    return Check(
        id="claude-cli", title="claude CLI",
        status=STATUS_PASS if authed else STATUS_FAIL,
        detail=f"{cli_version}, {'authenticated' if authed else 'NOT authenticated'}",
        fix="" if authed else "claude auth login",
    )


def _lore_store_check() -> Check:
    try:
        import lore_core
        from lore_core import store as lore_store

        root = lore_core.ROOT
        conn = lore_store.db_connect()
        count = conn.execute(
            "SELECT count(*) FROM beliefs WHERE status = 'active'"
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001 -- a broken store is information
        return Check(
            id="lore-store", title="LORE store", status=STATUS_FAIL,
            detail=f"could not open the store: {type(exc).__name__}: {exc}",
            fix="run /setup to choose or create a LORE store",
        )
    return Check(
        id="lore-store", title="LORE store", status=STATUS_PASS,
        detail=f"{root} -- {count} active belief(s)",
    )


def _config_check() -> Check:
    path = config_mod.config_path()
    if not path.exists():
        return Check(
            id="config", title="config file", status=STATUS_PASS,
            detail=f"{path} -- absent, every setting at its default",
        )
    try:
        with path.open("rb") as fh:
            tomllib.load(fh)
    except (OSError, ValueError) as exc:
        return Check(
            id="config", title="config file", status=STATUS_FAIL,
            detail=f"{path} -- {exc}",
            fix=f"edit {path} to fix the syntax, or delete it to reset to defaults",
        )
    return Check(
        id="config", title="config file", status=STATUS_PASS,
        detail=f"{path} -- parses cleanly",
    )


def _registry_check() -> Check:
    # NOT peers_mod.list_daemons() -- it calls read_registry(reap=True),
    # which DELETES stale entries as a side effect. A read-only health
    # check must never itself mutate the fleet it's reporting on; that is
    # exactly the bug count_stale (and this reap=False read) exist to
    # avoid.
    live = len([p for p in peers_mod.read_registry(reap=False) if p.daemon_socket])
    stale = peers_mod.count_stale()
    detail = f"{live} live daemon-hosted session(s)"
    if stale:
        detail += (
            f", {stale} stale presence file(s) -- report only, a normal "
            "launch's sweep removes these"
        )
    return Check(id="registry", title="daemon/registry health", status=STATUS_PASS, detail=detail)


def _image_protocol_check() -> Check:
    from . import images as images_mod

    mode = images_mod.detect_mode()
    forced = config_mod.raw("DOXA_IMAGE_MODE").strip()
    source = " (forced via DOXA_IMAGE_MODE)" if forced else " (auto-detected)"
    return Check(
        id="image-protocol", title="terminal image protocol", status=STATUS_PASS,
        detail=f"{mode}{source}",
    )


def _keyboard_enhancement_check() -> Check:
    return Check(
        id="keyboard-enhancement", title="keyboard enhancement", status=STATUS_UNKNOWN,
        detail=(
            "requested (kitty/CSI-u) at session start; whether THIS terminal "
            "granted it isn't measured yet"
        ),
    )


def _mcp_check() -> Check:
    # DOXA has no setting for an external MCP server yet -- only its own
    # in-process tool server (doxa.operators). Honest "nothing configured"
    # rather than a check standing in for a feature that doesn't exist.
    return Check(
        id="mcp", title="MCP reachability", status=STATUS_PASS,
        detail="no external MCP servers configured",
    )


CHECKS: "tuple[Callable[[], Check], ...]" = (
    _python_check,
    _doxa_version_check,
    _claude_cli_check,
    _lore_store_check,
    _config_check,
    _registry_check,
    _image_protocol_check,
    _keyboard_enhancement_check,
    _mcp_check,
)


def run_checks() -> list[Check]:
    """Every check, in report order. Blocking (the claude CLI probes shell
    out) -- callers run it off the event loop, same discipline
    doxa.naming.name_for documents for its own subprocess call."""
    return [check() for check in CHECKS]


def report(checks: "list[Check] | None" = None) -> str:
    checks = run_checks() if checks is None else checks
    lines = ["doctor: read-only health checks", ""]
    for check in checks:
        glyph = STATUS_GLYPH.get(check.status, "?")
        lines.append(f"{glyph} {check.title} -- {check.detail}")
        if check.fix:
            lines.append(f"    fix: {check.fix}")
    failing = [c for c in checks if c.status == STATUS_FAIL]
    lines.append("")
    lines.append(
        "all checks pass"
        if not failing
        else f"{len(failing)} check(s) failing -- see fix commands above"
    )
    return "\n".join(lines)


def any_failing(checks: "list[Check] | None" = None) -> bool:
    checks = run_checks() if checks is None else checks
    return any(c.status == STATUS_FAIL for c in checks)
