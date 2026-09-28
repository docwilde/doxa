#!/usr/bin/env python3
"""Record an actual DOXA terminal on an owned, private Xvfb display.

Requires system Python with GTK3/VTE, Xvfb and ffmpeg. A real authenticated
provider CLI is inherited from HOME; DOXA and LORE state are isolated.
No fixture events, test backend, or demo engine are used.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import threading

SIZE = (3068, 1734)


def control(args):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(args.control)
        connection.sendall((args.action + "\n").encode())
        reply = bytearray()
        while chunk := connection.recv(65536):
            reply.extend(chunk)
        print(reply.decode())


def run(args):
    state = Path(args.state).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("home", "runtime", "lore", "projects", "shots"):
        (state / name).mkdir(mode=0o700, exist_ok=True)
    read_fd, write_fd = os.pipe()
    log = (state / "xvfb.log").open("wb")
    display = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{SIZE[0]}x{SIZE[1]}x24", "-nolisten", "tcp"],
        pass_fds=(write_fd,), stdout=log, stderr=log,
    )
    os.close(write_fd)
    with os.fdopen(read_fd) as reader:
        display_number = reader.readline().strip()
    try:
        if not display_number:
            raise RuntimeError("Xvfb did not allocate a private display")
        _run_terminal(args, state, display_number)
    finally:
        display.terminate()
        display.wait(timeout=10)
        log.close()


def _run_terminal(args, state, display_number):
    with ExitStack() as cleanup:
        os.environ["DISPLAY"] = ":" + display_number
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import GLib, Gtk, Pango, Vte
        window = Gtk.Window()
        cleanup.callback(window.destroy)
        window.set_decorated(False)
        window.set_default_size(*SIZE)
        window.move(0, 0)
        terminal = Vte.Terminal()
        terminal.set_font(Pango.FontDescription("DejaVu Sans Mono 30"))
        terminal.set_scrollback_lines(10000)
        terminal.set_mouse_autohide(True)
        window.add(terminal)
        window.show_all()
        window.resize(*SIZE)
        os.environ.pop("NO_COLOR", None)
        environment = dict(os.environ)
        environment["COLORTERM"] = "truecolor"
        environment.update(
            DOXA_HOME=str(state / "home"), DOXA_RUNTIME_DIR=str(state / "runtime"),
            LORE_ROOT=str(state / "lore"), LORE_PROJECTS_DIR=str(state / "projects"), DOXA_LORE="0", DOXA_DAEMON_BIN=str(Path(args.daemon).resolve()),
            DOXA_LORE_RS=str(Path(args.lore).resolve()), TERM="xterm-256color",
        )
        argv = [str(Path(args.binary).resolve()), "new", "--engine", args.engine, "--linger", "600"]
        provider = args.provider_bin or shutil.which(args.engine)
        if not provider:
            raise RuntimeError(f"{args.engine} CLI is unavailable")
        argv += [f"--{args.engine}-bin", str(Path(provider).resolve())]
        if args.model:
            argv += ["--model", args.model]
        if args.session:
            argv = [str(Path(args.binary).resolve()), "attach", args.session]
        launch_error = []
        def spawned(_terminal, pid, error, _data=None):
            if error:
                launch_error.append(str(error))
                Gtk.main_quit()
            else:
                (state / "terminal.json").write_text(json.dumps({"pid": pid, "display": os.environ["DISPLAY"], "size": SIZE, "engine": args.engine, "binary": argv[0]}, indent=2))
        terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(Path(args.repo).resolve()), argv,
                             [f"{key}={value}" for key, value in environment.items()],
                             GLib.SpawnFlags.DEFAULT, None, None, -1, None, spawned, None)
        control_path = Path(args.control)
        if control_path.exists():
            raise RuntimeError(f"Control socket already exists: {control_path}")
        server = socket.socket(socket.AF_UNIX)
        cleanup.callback(server.close)
        server.bind(str(control_path))
        cleanup.callback(control_path.unlink, missing_ok=True)
        os.chmod(control_path, 0o600)
        server.listen()
        def perform(action, result, done):
            try:
                kind = action["kind"]
                if kind == "input":
                    terminal.feed_child(action["text"].encode())
                    result["ok"] = True
                elif kind == "text":
                    result["text"] = terminal.get_text(lambda *unused: True)[0]
                elif kind == "status":
                    result.update(columns=terminal.get_column_count(), rows=terminal.get_row_count(),
                                  cell_width=terminal.get_char_width(), cell_height=terminal.get_char_height())
                elif kind == "capture":
                    name = action["name"]
                    if not name.replace("-", "").isalnum():
                        raise ValueError("Capture names must be alphanumeric with hyphens")
                    output = state / "shots" / (name + ".png")
                    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0", "-video_size", f"{SIZE[0]}x{SIZE[1]}", "-i", os.environ["DISPLAY"] + ".0", "-frames:v", "1", str(output)], check=True)
                    result.update(ok=True, path=str(output))
                elif kind == "quit":
                    Gtk.main_quit()
                    result["ok"] = True
                else:
                    raise ValueError("Unknown control action")
            except Exception as error:
                result["error"] = str(error)
            done.set()
            return False
        def serve():
            while True:
                connection, _ = server.accept()
                with connection:
                    with connection.makefile("r") as reader:
                        action = json.loads(reader.readline())
                    result, done = {}, threading.Event()
                    GLib.idle_add(perform, action, result, done)
                    done.wait(30)
                    connection.sendall(json.dumps(result).encode())
        threading.Thread(target=serve, daemon=True).start()
        print(f"Private display ready; control socket: {control_path}", flush=True)
        Gtk.main()
        if launch_error:
            raise RuntimeError("Terminal launch failed: " + launch_error[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("run")
    for name in ("binary", "daemon", "lore", "repo", "state", "control"):
        launch.add_argument("--" + name, required=True)
    launch.add_argument("--engine", choices=("claude", "codex"), default="claude")
    launch.add_argument("--provider-bin", help="Absolute provider CLI executable")
    launch.add_argument("--model")
    launch.add_argument("--session", help="Reattach an actual session in the isolated runtime")
    send = commands.add_parser("control")
    send.add_argument("--control", required=True)
    send.add_argument("--action", required=True, help="JSON action: input, status, capture or quit")
    args = parser.parse_args()
    (run if args.command == "run" else control)(args)


if __name__ == "__main__":
    main()
