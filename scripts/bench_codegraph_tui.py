#!/usr/bin/env python3
"""Release TUI `/codegraph` latency through the production Crossterm loop.

Starts the release `bench_pty_frames` binary in a PTY. It runs the production
`doxa_tui::ui::run_with_frames` event loop (also behind `doxa-rs --demo`).
Fixture daemon `hello` frames attach one local session whose cwd is the
measured Git worktree. The harness types real `/codegraph` commands into the
prompt and times each Enter keypress until the PTY has delivered the bytes
after which the answer's first rows are on screen. A small VT screen model
replays the bytes only after each span, so parsing never delays a timed read.

Each TUI process measures one cold query, waits until the process is CPU-idle,
then measures eight novel prepared queries and one repeated query. Cold modes
alternate between processes:

* `switch`: the session first attaches in a non-Git directory, the command is
  typed, then the same session's cwd changes to the worktree and Enter follows.
  The in-process index is empty and the production 30-second root-change
  throttle keeps automatic preparation from running: a fresh complete scan.
* `attach`: the session attaches in the worktree, which starts automatic
  preparation at once; the command is typed and submitted immediately, so the
  query normally waits for that preparation.

`--evict-sources` fsyncs, `POSIX_FADV_DONTNEED`-evicts and mincore-checks every
owned Rust/Python source before each process. No daemon, model, network or
LORE write is involved; terminal emulator parsing and paint are outside the span.
"""
from __future__ import annotations

import argparse
import codecs
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import platform
import pty
import re
import select
import signal
import socket
import statistics
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 160, 48
ESCAPES = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[^\[\]()]")
# Ratatui emits only changed cells. The answer's second row replaces a blank
# "Scanning…" modal row, so this marker arrives contiguous once escape
# sequences are removed. The screen replay then decides visibility exactly.
DONE_MARKERS = (b"Worktree:", b"query failed", b"no partial answer", b"Select a local session",
                b"Wait for the current", b"Enlarge active pane")
PAGE_DOWN = b"\x1b[6~"
GIT_ROUTING = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX"}


class Screen:
    """Enough of a VT model for Ratatui's Crossterm backend output."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"
        self.seq = ""

    def clear(self, r0: int, c0: int, r1: int, c1: int) -> None:
        for r in range(max(0, r0), min(self.rows, r1 + 1)):
            start = c0 if r == r0 else 0
            end = c1 if r == r1 else self.cols - 1
            for c in range(max(0, start), min(self.cols, end + 1)):
                self.grid[r][c] = " "

    def csi(self, seq: str) -> None:
        final, body = seq[-1], seq[:-1]
        if body.startswith(("?", ">", "<", "=")):
            if final == "h" and "1049" in body.split(";"):
                self.clear(0, 0, self.rows - 1, self.cols - 1)
            return
        params = [int(p) if p.isdigit() else 0 for p in body.split(";")] if body else []
        first = params[0] if params else 0
        if final in "Hf":
            self.row = (params[0] if params and params[0] else 1) - 1
            self.col = (params[1] if len(params) > 1 and params[1] else 1) - 1
        elif final == "J":
            if first == 0:
                self.clear(self.row, self.col, self.rows - 1, self.cols - 1)
            elif first == 1:
                self.clear(0, 0, self.row, self.col)
            else:
                self.clear(0, 0, self.rows - 1, self.cols - 1)
        elif final == "K":
            if first == 0:
                self.clear(self.row, self.col, self.row, self.cols - 1)
            elif first == 1:
                self.clear(self.row, 0, self.row, self.col)
            else:
                self.clear(self.row, 0, self.row, self.cols - 1)
        elif final == "A":
            self.row = max(0, self.row - max(first, 1))
        elif final == "B":
            self.row = min(self.rows - 1, self.row + max(first, 1))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + max(first, 1))
        elif final == "D":
            self.col = max(0, self.col - max(first, 1))
        elif final == "G":
            self.col = max(first, 1) - 1
        elif final == "d":
            self.row = max(first, 1) - 1

    def feed(self, data: bytes) -> None:
        for ch in self.decoder.decode(data):
            state = self.state
            if state == "text":
                if ch == "\x1b":
                    self.state = "esc"
                elif ch == "\r":
                    self.col = 0
                elif ch == "\n":
                    self.row = min(self.rows - 1, self.row + 1)
                elif ch == "\b":
                    self.col = max(0, self.col - 1)
                elif ch >= " " and ch != "\x7f" and not unicodedata.combining(ch):
                    width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                    if 0 <= self.row < self.rows and 0 <= self.col < self.cols:
                        self.grid[self.row][self.col] = ch
                        if width == 2 and self.col + 1 < self.cols:
                            self.grid[self.row][self.col + 1] = ""
                    self.col += width
            elif state == "esc":
                self.state = {"[": "csi", "]": "osc", "P": "str", "_": "str", "^": "str",
                              "(": "charset", ")": "charset"}.get(ch, "text")
                self.seq = ""
            elif state == "csi":
                self.seq += ch
                if "@" <= ch <= "~":
                    self.csi(self.seq)
                    self.state = "text"
            elif state in ("osc", "str"):
                if ch == "\x07":
                    self.state = "text"
                elif ch == "\x1b":
                    self.state = state + "_esc"
            elif state in ("osc_esc", "str_esc"):
                self.state = "text" if ch == "\\" else state[:-4]
            elif state == "charset":
                self.state = "text"

    def text(self) -> str:
        return "\n".join("".join(row) for row in self.grid)


class Tui:
    def __init__(self, binary: Path, home: Path, socket_path: Path, cwd: Path, cpus: str | None) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        # The session cwd, not inherited repository routing, selects the worktree.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("DOXA_") and key not in GIT_ROUTING}
        env.update({"TERM": "xterm-256color", "DOXA_HOME": str(home), "DOXA_SKIP_FIRST_RUN": "1",
                    "DOXA_SKIP_UPDATE_CHECK": "1"})
        command = [str(binary), str(socket_path)]
        if cpus:
            command = ["taskset", "-c", cpus, *command]
        self.proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                     start_new_session=True)
        os.close(slave)
        self.fd = master
        self.screen = Screen(COLS, ROWS)
        self.socket_path = socket_path
        self.sock: socket.socket | None = None

    def read_available(self, wait: float) -> list[tuple[float, bytes]]:
        """Wait up to `wait` for output, then drain everything already queued,
        stamping each chunk as it is read."""
        chunks = []
        timeout = wait
        while select.select([self.fd], [], [], timeout)[0]:
            try:
                chunk = os.read(self.fd, 1 << 20)
            except OSError:
                break
            if not chunk:
                break
            chunks.append((time.perf_counter(), chunk))
            timeout = 0
        return chunks

    def wait_screen(self, predicate, timeout: float = 10.0, what: str = "screen state") -> None:
        deadline = time.perf_counter() + timeout
        while not predicate(self.screen.text()):
            if time.perf_counter() > deadline:
                raise RuntimeError(f"timed out waiting for {what}:\n{self.screen.text()}")
            for _, chunk in self.read_available(0.02):
                self.screen.feed(chunk)

    def settle(self, quiet: float = 0.05, timeout: float = 5.0) -> None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            chunks = self.read_available(quiet)
            if not chunks:
                return
            for _, chunk in chunks:
                self.screen.feed(chunk)

    def connect(self) -> None:
        deadline = time.monotonic() + 3
        sock = socket.socket(socket.AF_UNIX)
        while True:
            try:
                sock.connect(str(self.socket_path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.005)
        self.sock = sock

    def frame(self, value: dict) -> None:
        assert self.sock is not None
        self.sock.sendall(json.dumps(value, separators=(",", ":")).encode() + b"\n")

    def hello(self, cwd: Path) -> None:
        self.frame({"type": "hello", "session_id": "bench-codegraph", "model": "fixture", "cwd": str(cwd)})

    def type_command(self, command: str) -> None:
        os.write(self.fd, command.encode())
        self.wait_screen(lambda text: f"> {command}" in text, what=f"prompt echo {command!r}")

    def timed_enter(self, expect: str, timeout: float = 60.0, before_write=None) -> dict:
        """Write Enter; stamp every PTY chunk until a completion marker has
        arrived, collect the rest of that burst, then replay on the screen."""
        chunks: list[tuple[float, bytes]] = []
        raw = b""
        early = before_write() if before_write is not None else []
        started = time.perf_counter()
        os.write(self.fd, b"\r")
        deadline = started + timeout
        while True:
            got = self.read_available(0.05)
            chunks.extend(got)
            raw += b"".join(chunk for _, chunk in got)
            if any(marker in ESCAPES.sub(b"", raw) for marker in DONE_MARKERS):
                chunks.extend(self.read_available(0.03))
                break
            if time.perf_counter() > deadline:
                raise RuntimeError("no code graph answer before timeout")
        for _, chunk in early:
            self.screen.feed(chunk)
        visible = None
        for at, chunk in chunks:
            self.screen.feed(chunk)
            if visible is None and answer_visible(self.screen.text(), expect):
                visible = at
        if visible is None:
            raise RuntimeError(f"answer for {expect!r} not visible:\n{self.screen.text()}")
        burst_end = visible
        for at, _ in chunks:
            if visible < at <= burst_end + 0.002:
                burst_end = at
        row = {"query": expect, "first_output_ms": round((chunks[0][0] - started) * 1000, 3),
               "answer_visible_ms": round((visible - started) * 1000, 3),
               "answer_frame_last_byte_ms": round((burst_end - started) * 1000, 3),
               "output_bytes": sum(len(chunk) for _, chunk in chunks)}
        # Untimed: the production modal shows three rows; scroll one page to
        # read the origin label and coverage, then close the modal.
        if "query failed" not in self.screen.text():
            os.write(self.fd, PAGE_DOWN)
            self.wait_screen(lambda text: "Coverage:" in text, what="coverage row")
            self.settle(0.03)
        text = self.screen.text()
        row["origin"] = origin(text)
        row["coverage"] = coverage(text)
        os.write(self.fd, b"\x1b")
        self.wait_screen(lambda text: "Code graph ·" not in text, what="modal close")
        self.settle(0.03)
        return row

    def cpu_ticks(self) -> int:
        fields = Path(f"/proc/{self.proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])

    def wait_idle(self, window: float = 0.3, max_ticks: int = 1, timeout: float = 120.0) -> float:
        """Wait until automatic preparation and other background work stop:
        at most `max_ticks` process CPU ticks during one `window`."""
        started = time.perf_counter()
        before = self.cpu_ticks()
        while True:
            end = time.perf_counter() + window
            while (left := end - time.perf_counter()) > 0:
                for _, chunk in self.read_available(left):
                    self.screen.feed(chunk)
            after = self.cpu_ticks()
            if after - before <= max_ticks:
                return (time.perf_counter() - started) * 1000
            before = after
            if time.perf_counter() - started > timeout:
                raise RuntimeError("TUI did not become idle")

    def quit(self) -> None:
        try:
            os.write(self.fd, b"\x11")
            self.proc.wait(timeout=5)
        finally:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            if self.sock is not None:
                self.sock.close()
            os.close(self.fd)
        if self.proc.returncode:
            raise RuntimeError(f"TUI exited {self.proc.returncode}")


def answer_visible(text: str, expect: str) -> bool:
    if "query failed" in text:
        return True
    return f"{expect} ·" in text and "Worktree:" in text


def origin(text: str) -> str:
    if "query failed" in text:
        return "error"
    if "Reused parsed declarations" in text:
        return "indexed"
    if "Reused syntax answer" in text:
        return "revalidated"
    return "fresh"


def coverage(text: str) -> dict | None:
    found = re.search(r"Coverage: (\d+) listed · (\d+) parsed Rust · (\d+) parsed Python · (\d+) skipped · "
                      r"(\d+) unparseable", text)
    if not found:
        return None
    keys = ("listed", "parsed_rust", "parsed_python", "skipped", "unparseable")
    return dict(zip(keys, map(int, found.groups())))


def sources(root: Path) -> list[Path]:
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "--cached", "--others",
                                     "--exclude-standard", "-z"], timeout=10)
    return [root / os.fsdecode(name) for name in names.split(b"\0")
            if name and Path(os.fsdecode(name)).suffix in (".rs", ".py")]


def load_residency():
    spec = importlib.util.spec_from_file_location("bench_codegraph_cold", ROOT / "scripts/bench_codegraph_cold.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.residency


def resident(paths: list[Path], residency) -> list[int]:
    counts = [residency(path) for path in paths]
    return [sum(item[0] for item in counts), sum(item[1] for item in counts)]


def evict(paths: list[Path], residency) -> list[int]:
    for path in paths:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise RuntimeError("source eviction needs owned regular fixture sources")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    return resident(paths, residency)


def power_state() -> str:
    """Platform profile and P-state energy preference: both changed latency
    several-fold on the hybrid laptop used for the first receipt."""
    values = []
    for path in ("/sys/firmware/acpi/platform_profile",
                 "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"):
        try:
            values.append(Path(path).read_text().strip())
        except OSError:
            values.append("unknown")
    return "/".join(values)


def run_process(args, index: int, temp: Path, residency) -> dict:
    root = args.root
    mode = ("switch", "attach")[index % 2]
    home = temp / f"home-{index}"
    home.mkdir()
    outside = temp / f"outside-git-{index}"
    outside.mkdir()
    source_paths = sources(root) if args.evict_sources else []
    record: dict = {"process": index + 1, "cold_mode": mode}
    if args.evict_sources:
        pages = evict(source_paths, residency)
        if pages[0]:
            raise RuntimeError(f"source eviction incomplete: {pages[0]}/{pages[1]} pages")
    record["loadavg_before"] = [round(x, 2) for x in os.getloadavg()]
    record["power_before"] = power_state()
    tui = Tui(args.binary, home, temp / f"frames-{index}.sock", temp, args.cpus)
    transcript = "".join(f"- transcript line {i:03}: reproducible text\n" for i in range(160))
    try:
        started = time.perf_counter()
        tui.wait_screen(lambda text: "Prompt" in text, what="startup prompt")
        record["startup_prompt_ms"] = round((time.perf_counter() - started) * 1000, 3)
        tui.settle(0.05)
        tui.connect()

        def check_cold_sources() -> None:
            # Checked immediately before the worktree becomes the session cwd:
            # in attach mode automatic preparation reads sources from then on.
            if args.evict_sources:
                record["cold_resident_pages"] = resident(source_paths, residency)
                if record["cold_resident_pages"][0]:
                    raise RuntimeError("source pages became resident before the cold query")

        if mode == "attach":
            check_cold_sources()
        tui.hello(outside if mode == "switch" else root)
        tui.frame({"type": "event", "session_id": "bench-codegraph",
                   "event": {"type": "text_delta", "data": {"text": transcript}}})
        attached = time.perf_counter()
        tui.wait_screen(lambda text: "transcript line 159" in text, what="session attach")
        if mode == "switch":
            tui.wait_idle()  # The failed outside-Git preparation attempt has finished.
        cold = f"file {args.cold_file}"
        tui.type_command(f"/codegraph {cold}")

        def before_enter() -> list:
            if mode == "attach":
                record["attach_to_enter_ms"] = round((time.perf_counter() - attached) * 1000, 3)
                return []
            check_cold_sources()
            tui.hello(root)
            early = tui.read_available(2.0)  # Redraw proves the new cwd was applied.
            if not early:
                raise RuntimeError("cwd change produced no redraw")
            return early

        record["cold"] = tui.timed_enter(cold, before_write=before_enter)
        record["idle_wait_ms"] = round(tui.wait_idle(), 3)
        record["prepared"] = []
        rust, python = args.file, args.python_file
        missing = f"__doxa_tui_bench_missing_{index + 1}__"
        for query in (f"file {rust}", f"calls {rust}", f"imports {rust}", f"symbol {args.symbol}",
                      f"file {python}", f"calls {python}", f"imports {python}", f"symbol {missing}"):
            tui.type_command(f"/codegraph {query}")
            row = tui.timed_enter(query)
            if row["coverage"] != record["cold"]["coverage"]:
                raise RuntimeError("source inventory changed between queries")
            del row["coverage"]
            record["prepared"].append(row)
        tui.type_command(f"/codegraph file {rust}")
        record["repeat"] = tui.timed_enter(f"file {rust}")
        del record["repeat"]["coverage"]
    finally:
        tui.quit()
    record["loadavg_after"] = [round(x, 2) for x in os.getloadavg()]
    record["power_after"] = power_state()
    return record


def stats(values: list[float]) -> dict:
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(values[math.ceil(0.95 * len(values)) - 1], 3),
            "max_ms": round(values[-1], 3), "min_ms": round(values[0], 3)}


def summarize(records: list[dict]) -> dict:
    series = {
        "cold_switch": [r["cold"] for r in records if r["cold_mode"] == "switch"],
        "cold_attach": [r["cold"] for r in records if r["cold_mode"] == "attach"],
        "prepared": [row for r in records for row in r["prepared"]],
        "repeat": [r["repeat"] for r in records],
    }
    out = {}
    for name, rows in series.items():
        origins: dict[str, int] = {}
        for row in rows:
            origins[row["origin"]] = origins.get(row["origin"], 0) + 1
        out[name] = {"answer_visible": stats([row["answer_visible_ms"] for row in rows]),
                     "answer_frame_last_byte": stats([row["answer_frame_last_byte_ms"] for row in rows]),
                     "first_output": stats([row["first_output_ms"] for row in rows]),
                     "origins": origins}
    out["attach_to_enter"] = stats([r["attach_to_enter_ms"] for r in records if "attach_to_enter_ms" in r])
    out["power_states"] = sorted({r[key] for r in records for key in ("power_before", "power_after")})
    out["loadavg_1m"] = stats([r["loadavg_before"][0] for r in records] + [r["loadavg_after"][0] for r in records])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/bench_pty_frames")
    parser.add_argument("--root", type=Path, required=True, help="Git worktree queried by the session")
    parser.add_argument("--file", required=True, help="Rust source for prepared file/calls/imports queries")
    parser.add_argument("--python-file", required=True, help="Python source for prepared queries")
    parser.add_argument("--cold-file", required=True, help="Different Rust source for the cold file query")
    parser.add_argument("--symbol", required=True, help="Existing symbol for one prepared query")
    parser.add_argument("--runs", type=int, default=20, help="TUI processes; cold modes alternate")
    parser.add_argument("--evict-sources", action="store_true",
                        help="Evict and mincore-check every owned Rust/Python source before each process")
    parser.add_argument("--cpus", help="Optional taskset CPU list")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    args.binary = args.binary.resolve(strict=True)
    args.root = args.root.resolve(strict=True)
    if not 2 <= args.runs <= 200:
        parser.error("--runs must be 2..200")
    if args.cold_file == args.file:
        parser.error("--cold-file must differ from --file so prepared queries stay novel")
    residency = load_residency() if args.evict_sources else None
    records = []
    uptime_before = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="doxa-codegraph-tui-") as temp:
        for index in range(args.runs):
            records.append(run_process(args, index, Path(temp), residency))
    uptime_after = subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()
    print(json.dumps({
        "benchmark": "release_tui_codegraph_pty", "label": args.label, "root": str(args.root),
        "binary": str(args.binary), "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        "size": [COLS, ROWS], "runs": args.runs, "evict_sources": args.evict_sources, "cpus": args.cpus,
        "kernel": platform.release(), "uptime_before": uptime_before, "uptime_after": uptime_after,
        "span": "Enter byte written to PTY master -> PTY read after which the answer header rows are on screen",
        "summary": summarize(records), "records": records}, separators=(",", ":")))


if __name__ == "__main__":
    main()
