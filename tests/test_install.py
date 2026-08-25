# SPDX-License-Identifier: AGPL-3.0-only
"""scripts/install.sh -- the curl-pipe installer.

A shell script cannot be unit-tested by importing it, so these tests run
the real script with `sh` (dash, the strictest common /bin/sh) against a
FAKE PATH: tiny stand-in `python3`/`uv`/`git`/`claude`/`curl` executables
that record what they were called with and answer deterministically, no
network involved. `HOME` points at a throwaway directory for every run.

Two properties get their own tests because they were explicit design
requirements, not incidental:

* idempotency -- running the script twice must succeed both times.
* pipe-truncation safety -- the whole body lives inside `main() { ... };
  main "$@"` so a `curl | sh` pipe cut short at ANY point runs nothing.
  That is checked directly, by truncating the real script at several byte
  offsets and feeding each prefix to `sh` on stdin (the real invocation
  shape), asserting no side effect ever happens short of the full file.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "scripts" / "install.sh"

FAKE_PYPROJECT = 'requires-python = ">=3.11"\n'

# Real POSIX utilities install.sh shells out to, besides the five tools
# these tests fake out (python3/uv/git/claude/curl). A "missing git" test
# cannot just delete fakebin/git and leave /usr/bin on PATH -- the real
# system git would still answer. So PATH for every run is fakebin PLUS a
# directory of symlinks to ONLY these, never the raw system bin dirs.
_REAL_UTILS = ("sh", "cut", "sed", "grep", "tr", "mkdir", "chmod", "head")


def _utildir(tmp_path: Path) -> Path:
    import shutil

    target = tmp_path / "realutils"
    if not target.exists():
        target.mkdir()
        for name in _REAL_UTILS:
            found = shutil.which(name)
            assert found, f"test host is missing required utility: {name}"
            (target / name).symlink_to(found)
    return target


def _write_fake(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _fakebin(
    tmp_path: Path,
    *,
    uv_present: bool = True,
    claude_present: bool = True,
    claude_authed: bool = True,
    python_version: str = "3.12.3",
) -> Path:
    """A directory of fake tools plus a log file every fake appends to, so
    a test can assert exactly what the installer invoked."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")

    _write_fake(
        bindir / "python3",
        f"""
case "$1" in
  --version) echo "Python {python_version}" ;;
esac
""",
    )

    _write_fake(
        bindir / "git",
        f"""
echo "git $*" >> '{log}'
case "$1" in
  ls-remote) echo "deadbeefcafef00d1234567890abcdef12345678	refs/heads/main" ;;
esac
exit 0
""",
    )

    _write_fake(
        bindir / "curl",
        f"""
echo "curl $*" >> '{log}'
last=""
for a in "$@"; do last="$a"; done
case "$last" in
  *pyproject.toml*) printf '%s' '{FAKE_PYPROJECT}' ;;
  *astral.sh*)
    # Simulate the real astral installer's SIDE EFFECT (a `uv` binary
    # lands on PATH) directly, rather than emitting a script for the
    # caller's `| sh` to run -- avoids a shell-quoting-inside-a-shell-
    # quoting mess for no behavioural difference the installer can see.
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/uv" <<'UVEOF'
#!/bin/sh
echo "uv $*" >> "$DOXA_TEST_LOG"
case "$1" in --version) echo "uv 0.9.9 (fake)" ;; esac
exit 0
UVEOF
    chmod +x "$HOME/.local/bin/uv"
    ;;
  *) : ;;
esac
exit 0
""",
    )

    if claude_present:
        auth_exit = "0" if claude_authed else "1"
        _write_fake(
            bindir / "claude",
            f"""
echo "claude $*" >> '{log}'
case "$1 $2" in
  "auth status") exit {auth_exit} ;;
esac
exit 0
""",
        )

    if uv_present:
        _write_fake(
            bindir / "uv",
            f"""
echo "uv $*" >> '{log}'
case "$1" in
  --version) echo "uv 0.9.9" ;;
esac
exit 0
""",
        )

    return bindir


def _run(
    tmp_path: Path,
    bindir: Path,
    args: "list[str] | None" = None,
    *,
    timeout: float = 15.0,
    stdin_data: "str | None" = "",
) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:{_utildir(tmp_path)}",
        "DOXA_HOME": str(home / ".doxa"),
        "DOXA_TEST_LOG": str(tmp_path / "calls.log"),
    }
    cmd = ["sh", str(INSTALL_SH), *(args or [])]
    return subprocess.run(
        cmd,
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin_data,
        start_new_session=True,  # detach from the controlling tty, like curl|sh in CI
    )


# -- happy path -------------------------------------------------------------


def test_happy_path_installs_and_reports_version_and_commit(tmp_path):
    bindir = _fakebin(tmp_path)
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 0, proc.stderr
    assert "installer v1.0.0" in proc.stdout
    assert "resolved main -> deadbeefcafef00d" in proc.stdout
    assert (tmp_path / "home" / ".doxa").is_dir()
    log = (tmp_path / "calls.log").read_text()
    assert "uv tool install --force git+https://github.com/docwilde/doxa" in log
    assert "done. cd into a project and run: doxa" in proc.stdout


def test_doctor_reports_doxa_not_yet_on_path(tmp_path):
    """The fake `uv tool install` in these tests doesn't actually place a
    `doxa` binary anywhere real -- exactly the state a freshly installed
    shell is in until it's reopened, which is what this message is for."""
    bindir = _fakebin(tmp_path)
    proc = _run(tmp_path, bindir)
    assert "doxa doctor: doxa is not on PATH" in proc.stdout


def test_doctor_runs_for_real_when_on_path_even_if_a_check_fails(tmp_path):
    """A failing doctor check (exit 1) is real information the installer
    must still print -- and must NOT treat as its own failure, since the
    installer's actual job already succeeded a few lines up (`|| true`
    in scripts/install.sh)."""
    bindir = _fakebin(tmp_path)
    _write_fake(
        bindir / "doxa",
        f"""
echo "doxa $*" >> '{tmp_path / "calls.log"}'
if [ "$1" = "doctor" ]; then
  echo "doctor: read-only health checks"
  echo "x claude CLI -- NOT authenticated"
  exit 1
fi
exit 0
""",
    )
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 0, proc.stderr
    assert "doctor: read-only health checks" in proc.stdout
    assert "NOT authenticated" in proc.stdout
    assert "done. cd into a project and run: doxa" in proc.stdout


def test_never_overwrites_an_existing_doxa_home(tmp_path):
    bindir = _fakebin(tmp_path)
    home = tmp_path / "home"
    doxa_home = home / ".doxa"
    doxa_home.mkdir(parents=True)
    sentinel = doxa_home / "config.toml"
    sentinel.write_text("model = \"keep-me\"\n", encoding="utf-8")
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 0, proc.stderr
    assert sentinel.read_text() == "model = \"keep-me\"\n"
    assert "already exists -- left untouched" in proc.stdout


# -- idempotency --------------------------------------------------------


def test_idempotent_running_twice_both_succeed(tmp_path):
    bindir = _fakebin(tmp_path)
    first = _run(tmp_path, bindir)
    second = _run(tmp_path, bindir)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    log = (tmp_path / "calls.log").read_text()
    assert log.count("uv tool install --force") == 2
    assert (tmp_path / "home" / ".doxa").is_dir()


# -- version argument ---------------------------------------------------


def test_optional_version_arg_pins_the_ref(tmp_path):
    bindir = _fakebin(tmp_path)
    proc = _run(tmp_path, bindir, args=["v0.5.0"])
    assert proc.returncode == 0, proc.stderr
    assert "target ref: v0.5.0" in proc.stdout
    log = (tmp_path / "calls.log").read_text()
    assert "uv tool install --force git+https://github.com/docwilde/doxa@v0.5.0" in log
    assert "git ls-remote https://github.com/docwilde/doxa v0.5.0" in log


# -- prerequisite failures, each with its exact fix ----------------------


def test_missing_git_stops_with_a_fix(tmp_path):
    bindir = _fakebin(tmp_path)
    (bindir / "git").unlink()
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 1
    assert "install git" in proc.stderr
    assert not (tmp_path / "home" / ".doxa").exists()


def test_python_too_old_stops_with_a_fix(tmp_path):
    bindir = _fakebin(tmp_path, python_version="3.9.1")
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 1
    assert "found python3 3.9" in proc.stderr
    assert "3.11" in proc.stderr
    assert not (tmp_path / "home" / ".doxa").exists()


def test_missing_claude_cli_stops_with_a_fix(tmp_path):
    bindir = _fakebin(tmp_path, claude_present=False)
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 1
    assert "claude auth login" in proc.stderr
    assert "docs.claude.com" in proc.stderr
    assert not (tmp_path / "home" / ".doxa").exists()


def test_claude_present_but_not_authenticated_prints_exact_fix(tmp_path):
    bindir = _fakebin(tmp_path, claude_authed=False)
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 1
    assert proc.stderr.strip().endswith("claude auth login")
    assert not (tmp_path / "home" / ".doxa").exists()


def test_uv_missing_headless_defaults_to_installing_it(tmp_path):
    bindir = _fakebin(tmp_path, uv_present=False)
    proc = _run(tmp_path, bindir)
    assert proc.returncode == 0, proc.stderr
    assert "no controlling terminal" in proc.stderr
    assert "defaulting to 'y'" in proc.stderr
    log = (tmp_path / "calls.log").read_text()
    assert "curl -LsSf https://astral.sh/uv/install.sh" in log


def test_uv_missing_declined_on_a_real_tty(tmp_path):
    """The one prompt this installer has, answered 'n' over an actual
    controlling terminal -- proves _confirm reads a real tty correctly,
    not just that the headless default fires.

    `pty.fork()` (not `Popen` + an inherited pty fd) is what actually
    gives the child a CONTROLLING terminal: opening `/dev/tty` only
    resolves to a real device when the calling process already has one,
    which requires the pty's slave PATH to be open()'d by the child
    itself after setsid() -- exactly what pty.fork() does and a dup'd fd
    across Popen does not.
    """
    pytest.importorskip("pty")
    import pty
    import signal
    import time

    bindir = _fakebin(tmp_path, uv_present=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{_utildir(tmp_path)}",
        "DOXA_HOME": str(home / ".doxa"),
        "DOXA_TEST_LOG": str(tmp_path / "calls.log"),
    }

    pid, master_fd = pty.fork()
    if pid == 0:  # child -- now owns the new pty as its controlling terminal
        try:
            os.chdir(str(tmp_path))
            os.execvpe("sh", ["sh", str(INSTALL_SH)], env)
        except Exception:
            pass
        os._exit(127)

    status = None
    try:
        os.write(master_fd, b"n\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("installer did not exit within 10s on a real pty")
        output = b""
        try:
            while True:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                output += chunk
        except OSError:
            pass
    finally:
        os.close(master_fd)

    exit_code = (
        os.waitstatus_to_exitcode(status)
        if hasattr(os, "waitstatus_to_exitcode")
        else status >> 8
    )
    assert exit_code == 1, output
    assert b"uv is required" in output


# -- pipe-truncation safety -----------------------------------------------


SCRIPT_BYTES = INSTALL_SH.read_bytes()


@pytest.mark.parametrize(
    "fraction", [0.05, 0.15, 0.30, 0.45, 0.60, 0.75, 0.90, 0.97, 0.999]
)
def test_truncated_pipe_never_runs_a_partial_install(tmp_path, fraction):
    """`curl | sh` cut short at ANY point before the very end must be a
    no-op: no ~/.doxa, no uv invocation, no hang. This is what wrapping
    the whole script in `main() { ... }; main "$@"` buys."""
    bindir = _fakebin(tmp_path)
    cutoff = int(len(SCRIPT_BYTES) * fraction)
    truncated = SCRIPT_BYTES[:cutoff]
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:{_utildir(tmp_path)}",
        "DOXA_HOME": str(home / ".doxa"),
    }
    proc = subprocess.run(
        ["sh"],
        cwd=str(tmp_path),
        env=env,
        input=truncated,
        capture_output=True,
        timeout=10,
        start_new_session=True,
    )
    assert not (home / ".doxa").exists(), (
        f"fraction={fraction} produced side effects from a truncated pipe"
    )
    log_path = tmp_path / "calls.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "uv tool install" not in log


def test_full_script_via_stdin_is_the_positive_control(tmp_path):
    """Same shape as the truncation tests (script fed on stdin, exactly
    how `curl | sh` runs it) but the FULL script -- proves the truncation
    tests above are actually exercising a script that runs, not one that
    silently never does anything at any length."""
    bindir = _fakebin(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:{_utildir(tmp_path)}",
        "DOXA_HOME": str(home / ".doxa"),
    }
    proc = subprocess.run(
        ["sh"],
        cwd=str(tmp_path),
        env=env,
        input=SCRIPT_BYTES,
        capture_output=True,
        timeout=15,
        start_new_session=True,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert (home / ".doxa").is_dir()


# -- shell hygiene ----------------------------------------------------------


def test_script_is_valid_posix_sh_under_dash():
    """dash is the strictest common /bin/sh -- `dash -n` parses without
    executing, catching bashisms this script must not contain."""
    proc = subprocess.run(
        ["dash", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_script_is_executable():
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR
