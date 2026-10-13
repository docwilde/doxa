#!/usr/bin/env python3
"""Opt-in disposable-host PTY acceptance of the actual installed plugin TUI."""
from __future__ import annotations

import argparse
import codecs
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unicodedata

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().with_name("plugin-installed-host-proof.py")
spec = importlib.util.spec_from_file_location("installed_plugin_proof", SCRIPT)
assert spec is not None and spec.loader is not None
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
common = proof.common
ROOT = proof.ROOT


def private_write(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)


class Screen:
    """Bounded reconstruction of the cursor/erase operations ratatui emits."""
    def __init__(self, rows: int = 40, columns: int = 120):
        self.rows, self.columns = rows, columns
        self.cells = [[" " for _ in range(columns)] for _ in range(rows)]
        self.row = self.column = 0
        self.saved = (0, 0)
        self.pending = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, chunk: bytes) -> None:
        text = self.pending + self.decoder.decode(chunk)
        self.pending = ""
        index = 0
        while index < len(text):
            value = text[index]
            if value == "\x1b":
                if index + 1 == len(text):
                    break
                if text[index + 1] == "[":
                    match = re.match(r"\x1b\[([0-?]*)([ -/]*)([@-~])", text[index:])
                    if match is None:
                        break
                    self.csi(match.group(1), match.group(3))
                    index += len(match.group(0))
                    continue
                if text[index + 1] == "]":
                    match = re.search(r"\x07|\x1b\\", text[index + 2:])
                    if match is None:
                        break
                    index += 2 + match.end()
                    continue
                if text[index + 1] == "7":
                    self.saved = self.row, self.column
                elif text[index + 1] == "8":
                    self.row, self.column = self.saved
                index += 2
                continue
            if value == "\r":
                self.column = 0
            elif value == "\n":
                self.row += 1
                if self.row >= self.rows:
                    self.cells.pop(0)
                    self.cells.append([" "] * self.columns)
                    self.row = self.rows - 1
            elif value == "\b":
                self.column = max(0, self.column - 1)
            elif value == "\t":
                self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif ord(value) >= 32:
                if self.column >= self.columns:
                    self.column = 0
                    self.row = min(self.rows - 1, self.row + 1)
                if unicodedata.combining(value):
                    self.cells[self.row][max(0, self.column - 1)] += value
                else:
                    self.cells[self.row][self.column] = value
                    self.column += 1
                    if unicodedata.east_asian_width(value) in ("W", "F") and self.column < self.columns:
                        self.cells[self.row][self.column] = ""
                        self.column += 1
            index += 1
        self.pending = text[index:]
        common.require(len(self.pending) < 65536, "TUI escape sequence exceeded 64 KiB")

    def csi(self, parameters: str, final: str) -> None:
        if parameters.startswith("?"):
            if final == "h" and "1049" in parameters:
                self.cells = [[" "] * self.columns for _ in range(self.rows)]
            return
        values = [int(value) if value.isdecimal() else 0 for value in parameters.split(";")]
        number = values[0] or 1
        if final in ("H", "f"):
            self.row = min(self.rows - 1, max(0, number - 1))
            self.column = min(self.columns - 1, max(0, (values[1] if len(values) > 1 else 1) - 1))
        elif final == "A": self.row = max(0, self.row - number)
        elif final == "B": self.row = min(self.rows - 1, self.row + number)
        elif final == "C": self.column = min(self.columns - 1, self.column + number)
        elif final == "D": self.column = max(0, self.column - number)
        elif final == "G": self.column = min(self.columns - 1, max(0, number - 1))
        elif final == "d": self.row = min(self.rows - 1, max(0, number - 1))
        elif final == "J":
            if values[0] in (2, 3): self.cells = [[" "] * self.columns for _ in range(self.rows)]
            elif values[0] == 0:
                self.cells[self.row][self.column:] = [" "] * (self.columns - self.column)
                for row in range(self.row + 1, self.rows): self.cells[row] = [" "] * self.columns
        elif final == "K":
            if values[0] == 2: self.cells[self.row] = [" "] * self.columns
            elif values[0] == 0: self.cells[self.row][self.column:] = [" "] * (self.columns - self.column)
            elif values[0] == 1: self.cells[self.row][:self.column + 1] = [" "] * (self.column + 1)
        elif final == "s": self.saved = self.row, self.column
        elif final == "u": self.row, self.column = self.saved

    def text(self) -> str:
        return "\n".join("".join(row) for row in self.cells)


class Frontend:
    def __init__(self, frontend: Path, daemon: Path, cwd: Path, environment: dict[str, str]):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.process = subprocess.Popen([str(frontend), "--engine", "fixture", "--linger", "0"],
            cwd=cwd, env=environment, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        os.set_blocking(self.master, False)
        self.log = bytearray()
        self.screen = Screen()

    def read(self) -> None:
        if not select.select([self.master], [], [], 0.01)[0]:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN):
                return
            raise
        self.log.extend(data)
        self.screen.feed(data)
        common.require(len(self.log) <= 2 * 1024 * 1024, "TUI proof output exceeded 2 MiB")

    def send(self, data: bytes) -> int:
        self.read()
        mark = len(self.log)
        os.write(self.master, data)
        if data == b"\x1b":
            # Allow crossterm to resolve a lone Escape before the next slash
            # command; adjoining bytes would otherwise describe an Alt key.
            deadline = time.monotonic() + 0.08
            while time.monotonic() < deadline:
                self.read()
        return mark

    def await_text(self, text: str, since: int = 0, seconds: float = 5) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            if len(self.log) > since and text in self.screen.text():
                return
            common.require(self.process.poll() is None, "installed TUI exited before expected result")
        raise common.ProofError(f"installed TUI did not display {text!r}")

    def command(self, name: str) -> int:
        return self.send(f"/native-plugin run {name}\r".encode())

    def close(self) -> None:
        if self.process.poll() is None:
            self.send(b"\x11")  # Ctrl-Q, production owner/window shutdown path
            deadline = time.monotonic() + 5
            while self.process.poll() is None and time.monotonic() < deadline:
                self.read()  # Never block the frontend on its small PTY output buffer.
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
                raise common.ProofError("installed TUI failed to finish window-close cleanup")
        self.read()
        os.close(self.master)


def worker_started(parent: Path, frontend: Frontend) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for group in parent.iterdir():
            if group.name.startswith("doxa-plugin-"):
                pids = common.bounded_read(group / "cgroup.procs").splitlines()
                events = common.bounded_read(group / "cgroup.events").splitlines()
                if pids and "populated 1" in events:
                    common.require(all(pid.isdecimal() for pid in pids), "invalid installed worker cgroup PID")
                    return {"path":str(group),"populated":1,"pids":[int(pid) for pid in pids]}
        # Rendering each typed key can fill the PTY before Enter is reduced.
        # Drain it while observing admission, just as a real terminal does.
        frontend.read()
    state = []
    for task in Path(f"/proc/{frontend.process.pid}/task").iterdir():
        state.append({"tid":task.name,"name":common.bounded_read(task / "comm").strip(),
                      "wait":common.bounded_read(task / "wchan").strip()})
    print(json.dumps({"failed_admission_threads":state,"screen":frontend.screen.text()},sort_keys=True),flush=True)
    raise common.ProofError("installed TUI worker never entered its populated cgroup")


def await_cleanup(parent: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(path.name.startswith("doxa-plugin-") for path in parent.iterdir()):
            return
        time.sleep(0.005)
    raise common.ProofError("installed TUI retained a worker cgroup")


def require_source(source: dict[str, str]) -> None:
    common.require(common.source_identity(ROOT) == source,
                   "installed TUI proof source changed after initial observation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument("--daemon", type=Path, required=True)
    parser.add_argument("--receipt", required=True)
    options = parser.parse_args()
    common.require(os.environ.get("DOXA_PLUGIN_DISPOSABLE_HOST") == "1"
                   and os.environ.get("DOXA_PLUGIN_TUI_ACCEPTANCE") == "1",
                   "TUI proof requires explicit disposable-host and TUI acceptance switches")
    parent, scratch, _ = common.preflight()
    source = common.source_identity(ROOT)
    frontend_identity = proof.trusted_identity(options.frontend)
    worker_identity = proof.trusted_identity(options.frontend.with_name("doxa-plugin-worker"))
    bwrap_identity = proof.trusted_identity(common.BWRAP)
    receipt = common.receipt_name(options.receipt, scratch)
    common.require(not any((scratch / (options.receipt + suffix)).exists()
                   for suffix in ("", ".tui.log", ".host1.json", ".host2.json", ".host3.json")),
                   "TUI proof receipt or companion evidence already exists")
    cases: list[str] = []
    running_workers: list[dict[str, object]] = []
    logs = bytearray()
    frontend: Frontend | None = None
    with tempfile.TemporaryDirectory(prefix="np-", dir=scratch) as raw:
        base = Path(raw).resolve()
        home = base / "home"
        home.mkdir(mode=0o700)
        runtime = base / "runtime"
        runtime.mkdir(mode=0o700)
        cwd = base / "cwd"
        cwd.mkdir(mode=0o700)
        key = base / "operator.pem"
        subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                       check=True, capture_output=True, timeout=5)
        key.chmod(0o600)
        public = subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
                                check=True, capture_output=True, timeout=5).stdout[-32:].hex()
        header = bytes.fromhex("0061736d010000000105016000017f03020100070d0109646f78615f6d61696e0000")
        modules = {"demo":header + bytes.fromhex("0a0601040041110b"),
                   "spin":header + bytes.fromhex("0a0b01090003400c000b41000b")}
        config = f'[native_plugin_host]\noperator_public_key = "{public}"\nepoch = 1\n'
        for name, module in modules.items():
            package = home / "native-plugin-packages" / name
            package.mkdir(mode=0o700, parents=True)
            package.parent.chmod(0o700)
            manifest = (f'package_api_version = 1\nname = "{name}"\nversion = "1"\n'
                        'artifact_format = "wasm-core-v1"\nrequested_grants = []\n').encode()
            private_write(package / "manifest.toml", manifest)
            private_write(package / "module.wasm", module)
            config += (f'\n[[native_plugin_packages]]\nname = "{name}"\n'
                       f'manifest_sha256 = "{hashlib.sha256(manifest).hexdigest()}"\n'
                       f'module_sha256 = "{hashlib.sha256(module).hexdigest()}"\ngrants = []\n')
        private_write(home / "config.toml", config.encode())
        environment = {key:value for key,value in os.environ.items()
                       if key in ("PATH","HOME","TMPDIR","CARGO_TARGET_DIR","CARGO_HOME","RUSTUP_HOME")}
        environment.update(TERM="xterm-256color", DOXA_HOME=str(home), DOXA_RUNTIME_DIR=str(runtime),
                           DOXA_DAEMON_BIN=str(options.daemon), DOXA_PLUGIN_INSTALLED_HOST_ACCEPTANCE="1",
                           DOXA_PLUGIN_OPERATOR_GO="1", CARGO_BUILD_JOBS="2", DOXA_SKIP_FIRST_RUN="1")

        def activate(number: int, lifetime: int = 900) -> Path:
            require_source(source)
            args = [sys.executable, str(SCRIPT), "--run", "--activate", "--frontend", str(options.frontend),
                "--expected-frontend-sha256", frontend_identity["sha256"],
                "--expected-worker-sha256", worker_identity["sha256"],
                "--expected-bwrap-sha256", bwrap_identity["sha256"],
                "--receipt", f"{options.receipt}.host{number}.json", "--tui-pid", str(frontend.process.pid),
                "--home", str(home), "--operator-key", str(key), "--lifetime-seconds", str(lifetime)]
            status, output = common.run_bounded(args, cwd=ROOT, environment=environment,
                limit=common.BUILD_LOG_LIMIT, timeout=common.BUILD_TIMEOUT_SECONDS + 60)
            logs.extend(output)
            sys.stdout.buffer.write(output)
            sys.stdout.flush()
            common.require(status == 0, "fresh installed proof or signed activation failed")
            require_source(source)
            tokens = list((home / "native-plugin-authority").glob("*.json"))
            common.require(len(tokens) == 1, "activation did not issue exactly one instance permit")
            return tokens[0]

        try:
            frontend = Frontend(options.frontend, options.daemon, cwd, environment)
            frontend.await_text("fixture", seconds=10)
            mark = frontend.command("demo")
            frontend.await_text("TUI execution disabled", mark)
            cases.append("no-permit-refused")
            frontend.send(b"\x1b")
            token = activate(1)
            mark = frontend.command("demo")
            frontend.await_text("Isolated plugin returned 17", mark)
            await_cleanup(parent)
            cases.append("signed-tui-return")
            frontend.send(b"\x1b")
            mark = frontend.command("spin")
            running_workers.append({"case":"unlink-revocation",**worker_started(parent, frontend)})
            token_bytes = token.read_bytes()
            token.unlink()
            frontend.await_text("Run cancelled", mark)
            await_cleanup(parent)
            cases.append("unlink-revokes-running-worker")
            frontend.send(b"\x1b")
            mark = frontend.command("demo")
            frontend.await_text("TUI execution disabled", mark)
            cases.append("revoked-next-run-refused")
            frontend.send(b"\x1b")
            token = activate(2)
            frontend.command("spin")
            running_workers.append({"case":"panel-close",**worker_started(parent, frontend)})
            frontend.send(b"\x1b")
            await_cleanup(parent)
            cases.append("panel-close-cleans-worker")
            mark = frontend.command("demo")
            frontend.await_text("Isolated plugin returned 17", mark)
            frontend.send(b"\x1b")
            frontend.command("spin")
            running_workers.append({"case":"window-close",**worker_started(parent, frontend)})
            frontend.close()
            logs.extend(frontend.log)
            frontend = None
            await_cleanup(parent)
            cases.append("window-close-cleans-worker")

            # Copying a previously valid signature under the new PID filename
            # must not authorize the new frontend instance.
            token.unlink()
            frontend = Frontend(options.frontend, options.daemon, cwd, environment)
            frontend.await_text("fixture", seconds=10)
            stat = common.bounded_read(Path(f"/proc/{frontend.process.pid}/stat"))
            ticks = stat.rpartition(")")[2].split()[19]
            forged = token.parent / f"{frontend.process.pid}-{ticks}.json"
            private_write(forged, token_bytes)
            mark = frontend.command("demo")
            frontend.await_text("TUI execution disabled", mark)
            cases.append("different-instance-refused")
            frontend.send(b"\x1b")
            activate(3, lifetime=1)
            time.sleep(1.2)
            mark = frontend.command("demo")
            frontend.await_text("TUI execution disabled", mark)
            await_cleanup(parent)
            cases.append("expired-permit-refused")
        finally:
            if frontend is not None:
                frontend.close()
                logs.extend(frontend.log)
            # Only groups belonging to our explicit test frontend, never a
            # live owner installation or unrelated proof service.
            await_cleanup(parent)
            private_write(scratch / f"{options.receipt}.tui.log", bytes(logs))
    require_source(source)
    common.write_receipt(receipt, {"format_version":1,"purpose":"disposable installed TUI acceptance",
        "source":source, "frontend":frontend_identity,
        "worker":worker_identity,"bwrap":bwrap_identity,"cases":cases,"running_workers":running_workers,
        "log_sha256":hashlib.sha256(logs).hexdigest(),"worker_cgroups_remaining":0})
    print("plugin-tui-acceptance " + " ".join(f"{case}=passed" for case in cases), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, common.ProofError, subprocess.SubprocessError) as exc:
        print(f"installed-plugin-tui-proof unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
