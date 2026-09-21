# SPDX-License-Identifier: AGPL-3.0-only
"""Headless Chrome over the DevTools protocol, on the standard library
alone -- the one browser driver this repository has.

WHY THIS FILE EXISTS RATHER THAN A SECOND COPY OF THE CLASS. It was born
inside ``scripts/mesh_shot.py``, which needed a browser because the mesh
graph is drawn on a ``<canvas>`` and a Textual pilot cannot photograph
one. ``tests/test_mesh_page.py`` then needed the same operations -- go
somewhere, ask the page a question, click, press a key, take the frame --
against the same page and the same real
:class:`doxa.meshgraph.MeshServer`. Two
copies of a transport are two places a timeout is tuned and one place it
is fixed, so the class moved here and both import it.

WHY ``scripts/`` AND NOT ``doxa/`` OR ``tests/``. Under ``doxa/`` it
would ride into the wheel, and a terminal application has no business
shipping a browser driver to users who will never launch one. Under
``tests/`` the import would run the wrong way: ``scripts/mesh_shot.py``
regenerates a committed gallery asset and must not depend on the test
tree to do it. ``scripts/`` is where the repository already keeps its
developer tooling, it is already importable from the suite (``pytest``'s
``pythonpath = ["."]``, and ``tests/test_screenshot_driver.py`` has
imported ``scripts.mesh_shot`` and ``scripts.screenshot`` since v1.7.1),
and neither consumer has to reach across a boundary to get here.

NO DEPENDENCY, AND THE PIPE TRANSPORT IS WHY. CDP is usually spoken over
a websocket, which would mean a package; over the PIPE transport it needs
nothing at all. Chrome reads commands on fd 3 and writes replies and
events on fd 4, each message a JSON object followed by a NUL byte, and
``json`` and ``os.read`` are the whole client. That is the same reason
``assets/mesh/mesh.js`` writes its own force layout rather than vendoring
d3: this repository adds no build step and no package manifest for a
view, and it adds none for the driver of that view either.

WHAT IS DELIBERATELY NOT HERE. No waiting-for-selector, no element
handles, no automatic retry -- the two consumers want different waits
(the screenshot waits on pixels, the suite waits on the page's own DOM)
and a shared guess at what "ready" means would be wrong for both.
:meth:`Chrome.until` polls one expression the caller writes, and that is
the whole scheduling story.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

__all__ = ["BROWSER_ENV", "CHROME", "CdpError", "Chrome", "chrome_binary"]

#: Where Chrome is on the machine this was written for. Tried first, then
#: ``PATH`` -- see :func:`chrome_binary`.
CHROME = "/usr/bin/google-chrome"

#: Names the browser to drive, overriding everything below. Set it to a
#: path that does not exist to make this machine look like one with no
#: browser at all, which is how the suite's skip path gets exercised.
BROWSER_ENV = "DOXA_CHROME"

#: The other names the same browser ships under. Chromium is enough for
#: everything either consumer does: no DRM, no proprietary codec, just a
#: renderer and the protocol.
CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)

#: How many protocol events to keep before dropping the oldest. Events
#: arrive on the same pipe as replies and most of them are nobody's
#: business, but a console message the page logged while a command was in
#: flight has to survive until the caller asks for it. A page that logs in
#: a ``requestAnimationFrame`` loop would otherwise grow this without
#: bound for as long as the browser is up -- which, in the suite, is the
#: whole session.
MAX_EVENTS = 2000

#: ``key``/``code``/virtual-key for the keys a caller may press by name.
#: A table rather than a general "type this string": the two consumers
#: press exactly these, and a wrong ``windowsVirtualKeyCode`` is a key
#: event Chrome delivers and the page ignores -- which looks like the page
#: being broken rather than the driver.
KEYS = {
    "Home": ("Home", "Home", 36),
    "End": ("End", "End", 35),
    "Escape": ("Escape", "Escape", 27),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
}


#: The child's first instruction: put this pipe on fd 3 and that one on
#: fd 4, then become Chrome.
#:
#: There is no ``subprocess`` argument for "hand the child this
#: descriptor AS number 3", and the two obvious ways round it are both
#: traps.
#:
#: A ``preexec_fn`` runs between fork and exec in a process that may hold
#: locks other threads were using -- and this driver is started next to a
#: :class:`doxa.meshgraph.MeshServer`, which is threaded. CPython
#: documents that as unsafe and it deadlocks for real.
#:
#: A one-line shell (``exec 3<&7 4>&10; exec "$@"``) was what this used
#: instead, and it worked until it did not: ``/bin/sh`` on Debian and
#: Ubuntu is ``dash``, whose redirections accept ONE digit. ``os.pipe``
#: returns whatever is free, which in a bare script is 3..6 and under
#: pytest was 7 and 10 -- so the suite met ``Syntax error: Bad fd
#: number`` and a browser that exited before answering, on a machine
#: where the same code had worked from a terminal all day.
#:
#: So the hop is Python, which has no such limit. Both ends are first
#: duplicated ABOVE 4 (``F_DUPFD`` returns the lowest free descriptor at
#: or above the number given), so neither assignment can destroy the
#: other's source; ``os.dup2`` then places them exactly, clearing
#: ``FD_CLOEXEC`` as it goes -- which is the whole point, and the thing
#: ``dup2`` onto a descriptor's OWN number silently fails to do. It costs
#: one interpreter start per browser, which is once per suite.
MOVE_FDS = (
    "import fcntl, os, sys\n"
    "r = fcntl.fcntl(int(sys.argv[1]), fcntl.F_DUPFD, 5)\n"
    "w = fcntl.fcntl(int(sys.argv[2]), fcntl.F_DUPFD, 5)\n"
    "os.dup2(r, 3)\n"
    "os.dup2(w, 4)\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)


class CdpError(RuntimeError):
    """The browser refused, answered with an error, or stopped answering.

    An ordinary exception rather than ``SystemExit``, which is what this
    code raised while it lived inside a script: a test that provokes one
    should fail with a traceback naming the expression, and only a
    command-line entry point should turn that into an exit status.
    ``scripts/mesh_shot.py`` does exactly that at its ``__main__`` guard,
    so its behaviour from a terminal is unchanged."""


def chrome_binary(preferred: "str | None" = None) -> "str | None":
    """An executable Chrome, or ``None`` if this machine has none.

    ``None`` rather than a raise, because both callers want to decide for
    themselves what the absence means: the screenshot script cannot do
    its job without a browser and says so; the suite skips. Nothing here
    may turn a machine with no Chrome into a failure by itself.

    :data:`BROWSER_ENV` wins over everything, and wins even when it names
    something that is not there. That is the point of it: it is how a
    machine whose browser is somewhere else says so, AND how a run
    proves what it does without one -- ``DOXA_CHROME=/nowhere uv run
    pytest tests/test_mesh_page.py`` is the whole browser suite on its
    skip path, which is otherwise only reachable by uninstalling
    Chrome."""
    override = os.environ.get(BROWSER_ENV)
    if override is not None:
        return override if os.access(override, os.X_OK) else None
    for candidate in (preferred, CHROME):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class Chrome:
    """One headless browser, driven over the CDP pipe transport.

    Owns the process: construct it, use it, :meth:`close` it. The profile
    directory is a required argument and should be a throwaway, because a
    driver may not read or write the browser session of whoever is
    running it and headless Chrome otherwise uses the default one."""

    def __init__(
        self,
        profile: Path,
        *,
        url: str = "about:blank",
        window: "tuple[int, int]" = (1280, 800),
        scale: int = 1,
        binary: "str | None" = None,
        extra_args: "tuple[str, ...]" = (),
    ) -> None:
        self.session = ""
        self.window = window
        self.scale = scale
        self._events: "list[dict]" = []

        executable = chrome_binary(binary)
        if executable is None:
            override = os.environ.get(BROWSER_ENV)
            raise CdpError(
                f"no browser at ${BROWSER_ENV}={override!r}"
                if override is not None else
                "no Chrome on this machine: tried "
                f"{CHROME} and {', '.join(CANDIDATES)} on PATH"
            )

        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        self._proc = subprocess.Popen(
            [
                sys.executable, "-c", MOVE_FDS,
                str(to_chrome_r), str(from_chrome_w), executable,
                "--headless=new", "--remote-debugging-pipe", "--disable-gpu",
                "--hide-scrollbars", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking",
                f"--user-data-dir={profile}",
                f"--window-size={window[0]},{window[1]}",
                *extra_args,
                "about:blank",
            ],
            pass_fds=(to_chrome_r, from_chrome_w),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._write, self._read = to_chrome_w, from_chrome_r
        self._buffer = b""
        self._next = 0

        page = next(
            target for target in self.call("Target.getTargets")["targetInfos"]
            if target["type"] == "page"
        )
        self.session = self.call(
            "Target.attachToTarget",
            {"targetId": page["targetId"], "flatten": True},
        )["sessionId"]
        self.call("Runtime.enable")
        self.call("Page.enable")
        # The viewport is set here rather than left to the window, so a
        # frame is exactly the geometry asked for whatever a window
        # manager, a screen, or the absence of both would have made it.
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": window[0], "height": window[1],
            "deviceScaleFactor": scale, "mobile": False,
        })
        self.navigate(url)

    # -- lifecycle --

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.call("Browser.close", timeout=5)
        except CdpError:
            self._proc.kill()
        self._proc.wait(timeout=10)

    # -- transport --

    def call(self, method: str, params: "dict | None" = None,
             timeout: float = 30.0) -> dict:
        self._next += 1
        message: "dict" = {"id": self._next, "method": method,
                           "params": params or {}}
        if self.session:
            message["sessionId"] = self.session
        os.write(self._write, json.dumps(message).encode("utf-8") + b"\0")
        deadline = time.time() + timeout
        while True:
            reply = self._recv(deadline)
            # Events stream down the same pipe. They are not the answer to
            # this command, so they are set aside for `take` rather than
            # dropped -- a console message the page logged is often the
            # only evidence of what it did.
            if reply.get("id") != self._next:
                if "method" in reply:
                    self._events.append(reply)
                    if len(self._events) > MAX_EVENTS:
                        del self._events[: len(self._events) - MAX_EVENTS]
                continue
            if "error" in reply:
                raise CdpError(f"{method}: {reply['error']}")
            return reply.get("result", {})

    def take(self, method: "str | None" = None) -> "list[dict]":
        """Buffered protocol events, removed from the buffer.

        Removed rather than merely read, because every caller so far asks
        the same question -- "did anything happen since I last looked" --
        and a buffer that keeps its history answers a different one."""
        if method is None:
            kept, taken = [], list(self._events)
        else:
            kept = [e for e in self._events if e.get("method") != method]
            taken = [e for e in self._events if e.get("method") == method]
        self._events = kept
        return taken

    def _recv(self, deadline: float) -> dict:
        while b"\0" not in self._buffer:
            if time.time() > deadline:
                raise CdpError("chrome stopped answering")
            chunk = os.read(self._read, 1 << 16)
            if not chunk:
                raise CdpError("chrome closed the devtools pipe")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(raw)

    # -- the page --

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def ask(self, expression: str):
        """One JavaScript expression, evaluated in the page, by value.

        Evaluated in the page's own global scope, which is why this can
        read ``assets/mesh/mesh.js``'s top-level ``const`` bindings:
        a classic script's top-level ``let``/``const`` land in the global
        LEXICAL environment, shared with every later global evaluation in
        the realm, even though they never become properties of
        ``window``. A test can therefore ask the renderer what it thinks
        rather than inferring it from pixels."""
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
        })
        if "exceptionDetails" in result:
            raise CdpError(
                f"{expression}: "
                f"{result['exceptionDetails'].get('text', 'threw')}"
            )
        return result.get("result", {}).get("value")

    def until(self, expression: str, what: str, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ask(expression):
                return
            time.sleep(0.05)
        raise CdpError(f"the page never reached: {what}")

    def click(self, x: float, y: float) -> None:
        """A real pointer press and release, at CSS-pixel coordinates.

        ``Input.dispatchMouseEvent`` becomes a real ``pointerdown`` in the
        renderer -- the same event a mouse produces, and the one
        ``assets/mesh/mesh.js`` listens for. Nothing here dispatches a
        synthetic DOM event the page would never see from a user. No
        movement between press and release, so a press on a canvas
        background pans by nothing and a press on a node pins it for
        exactly as long as the button is down."""
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left",
                "buttons": 1 if kind == "mousePressed" else 0,
                "clickCount": 1, "pointerType": "mouse",
            })

    def hover(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "pointerType": "mouse",
        })

    def press(self, name: str) -> None:
        """One named key, down and up, at whatever has focus."""
        try:
            key, code, virtual = KEYS[name]
        except KeyError:
            raise CdpError(f"no virtual key code for {name!r}") from None
        for kind in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind, "key": key, "code": code,
                "windowsVirtualKeyCode": virtual,
                "nativeVirtualKeyCode": virtual,
            })

    def frame(self) -> bytes:
        return base64.b64decode(
            self.call("Page.captureScreenshot", {"format": "png"})["data"]
        )
