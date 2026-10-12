#!/usr/bin/env python3
"""Capture the native App in a real GTK/VTE terminal with illustrative scene data.

Uses a private Xvfb display and isolated state; never contacts providers. Images
come from ffmpeg's display capture, without rasterizing or editing cell buffers.
Requires system Python with GTK3/VTE, Xvfb, ffmpeg and DejaVu Sans Mono.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent
SIZE = (3068, 1734)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "assets/shots")
    parser.add_argument("--icon-font", type=Path)
    parser.add_argument("scenes", nargs="*")
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_mode & 0o077:
        raise ValueError("Capture state must be owner-private")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = args.binary.resolve(strict=True)
    version = tomllib.loads((ROOT / "rust/doxa-tui/Cargo.toml").read_text())["package"]["version"]
    manifest = json.loads((ROOT / "docs/gallery/scenes.json").read_text())
    scenes = args.scenes or [scene["id"] for scene in manifest["scenes"]]
    if not scenes or any(name not in {s["id"] for s in manifest["scenes"]} for name in scenes):
        raise ValueError("Use scene identifiers from the gallery manifest")
    read_fd, write_fd = os.pipe()
    display = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0",
        f"{SIZE[0]}x{SIZE[1]}x24", "-nolisten", "tcp"], pass_fds=(write_fd,),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write_fd)
    with os.fdopen(read_fd) as reader:
        display_name = ":" + reader.readline().strip()
    child = []
    try:
        os.environ["DISPLAY"] = display_name
        os.environ.pop("NO_COLOR", None)
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Vte", "2.91")
        from gi.repository import GLib, Gtk, Pango, Vte
        if args.icon_font:
            fonts = ctypes.CDLL("libfontconfig.so.1")
            fonts.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            fonts.FcConfigAppFontAddFile.restype = ctypes.c_int
            if not fonts.FcConfigAppFontAddFile(None, os.fsencode(args.icon_font.resolve(strict=True))):
                raise RuntimeError("Could not register the private icon font")
        window = Gtk.Window()
        window.set_decorated(False)
        window.set_default_size(*SIZE)
        container = Gtk.Fixed()
        window.add(container)
        terminal = Vte.Terminal()
        terminal.set_font(Pango.FontDescription("DejaVu Sans Mono 30"))
        terminal.set_cell_height_scale(1.125)
        terminal.set_size_request(3024, 1674)
        container.put(terminal, 22, 30)
        window.show_all()
        recordings = []
        failure = []
        active = {"index": -1, "captured": False, "last": None, "stable": 0, "start": 0}

        def launch():
            active["index"] += 1
            if active["index"] == len(scenes):
                Gtk.main_quit()
                return False
            name = scenes[active["index"]]
            home = state / name
            home.mkdir(mode=0o700, exist_ok=True)
            env = {"PATH": os.environ["PATH"], "DISPLAY": display_name, "HOME": str(home),
                "DOXA_HOME": str(home / "doxa"), "LORE_ROOT": str(home / "lore"),
                "DOXA_RUNTIME_DIR": str(home / "runtime"), "TMPDIR": str(state),
                "TERM": "xterm-256color", "COLORTERM": "truecolor", "LC_ALL": "C.UTF-8",
                "DOXA_LORE": "0", "DOXA_NERD_FONT": "1" if args.icon_font else "0", "DOXA_CLOCK_SHOW": "0"}
            if active["index"] == 0:
                frame = json.loads(subprocess.check_output([str(binary), name], cwd=ROOT, env=env, timeout=30))
                if frame["version"] != version:
                    raise RuntimeError("Capture binary does not match the frontend version")
            active.update(captured=False, last=None, stable=0, start=time.monotonic())
            def spawned(_terminal, pid, error, _data=None):
                if error:
                    failure.append(str(error)); Gtk.main_quit()
                else:
                    child[:] = [pid]
            terminal.spawn_async(Vte.PtyFlags.DEFAULT, str(ROOT), [str(binary), name, "--terminal"],
                [f"{key}={value}" for key, value in env.items()], GLib.SpawnFlags.DEFAULT,
                None, None, -1, None, spawned, None)
            return False

        def exited(_terminal, status):
            child.clear()
            if status != 0 or not active["captured"]:
                failure.append(f"Scene exited before capture or with status {status}")
                Gtk.main_quit()
            else:
                GLib.idle_add(launch)

        def capture_when_stable():
            try:
                if active["captured"] or active["index"] < 0:
                    return True
                if time.monotonic() - active["start"] > 20:
                    raise RuntimeError("Scene did not stabilize before the capture deadline")
                text = terminal.get_text_format(Vte.Format.TEXT)
                active["stable"] = active["stable"] + 1 if text == active["last"] else 0
                active["last"] = text
                if not child or active["stable"] < 3 or time.monotonic() - active["start"] < 1.2:
                    return True
                name = scenes[active["index"]]
                path = output / f"rust-{version}-{name}.png"
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0",
                    "-video_size", f"{SIZE[0]}x{SIZE[1]}", "-i", display_name + ".0", "-frames:v", "1", str(path)], check=True)
                data = path.read_bytes()
                if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != SIZE:
                    raise RuntimeError("Invalid terminal capture dimensions")
                (state / f"{name}.txt").write_text(text)
                recordings.append({"scene": name, "image": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                    "columns": terminal.get_column_count(), "rows": terminal.get_row_count()})
                active["captured"] = True
                print(f"Captured {name} in GTK/VTE", flush=True)
                terminal.feed_child(b"\x03")
            except Exception as error:
                failure.append(str(error)); Gtk.main_quit(); return False
            return True

        terminal.connect("child-exited", exited)
        GLib.idle_add(launch)
        GLib.timeout_add(100, capture_when_stable)
        Gtk.main()
        window.destroy()
        if failure:
            raise RuntimeError("; ".join(failure))
        (output / f"rust-{version}-capture.json").write_text(json.dumps({"version": version,
            "method": "native production App in GTK3/VTE on private Xvfb; ffmpeg x11grab",
            "scene_data": "isolated illustrative fixtures; no providers, owner sessions or owner memory",
            "size": SIZE, "captures": recordings}, indent=2) + "\n")
    finally:
        for pid in child:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        display.terminate()
        display.wait(timeout=10)


if __name__ == "__main__":
    main()
