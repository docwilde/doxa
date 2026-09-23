# SPDX-License-Identifier: AGPL-3.0-only
"""Browser authentication delegated to the installed provider CLIs.

The CLI owns OAuth and its credential store. DOXA captures only public
progress (an authorization URL or device code); arbitrary CLI output is
never placed in the transcript.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import pty
import re
import select
import shutil
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit


class AuthError(RuntimeError):
    """Unknown provider, or a provider whose CLI is not installed."""


@dataclass(frozen=True)
class AuthProvider:
    name: str
    label: str
    login_cmd: tuple[str, ...]
    logout_cmd: tuple[str, ...]
    probe_cmd: tuple[str, ...]

    @property
    def binary(self) -> str:
        return self.login_cmd[0]

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def command_for(self, verb: str) -> tuple[str, ...]:
        if verb == "login":
            return self.login_cmd
        if verb == "logout":
            return self.logout_cmd
        if verb == "probe":
            return self.probe_cmd
        raise AuthError(f"unknown auth verb: {verb!r}")


PROVIDERS: dict[str, AuthProvider] = {
    "claude": AuthProvider(
        "claude", "Claude (Anthropic)", ("claude", "auth", "login"),
        ("claude", "auth", "logout"), ("claude", "auth", "status"),
    ),
    "codex": AuthProvider(
        "codex", "Codex (OpenAI)", ("codex", "login"),
        ("codex", "logout"), ("codex", "login", "status"),
    ),
}
DEFAULT_PROVIDER = "claude"
AUTH_LOCK = asyncio.Lock()
AUTH_TIMEOUT_SECONDS = 15 * 60


def provider_names() -> list[str]:
    return list(PROVIDERS)


def installed_names() -> list[str]:
    return [name for name, row in PROVIDERS.items() if row.installed()]


def resolve(name: str | None) -> AuthProvider:
    key = (name or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    row = PROVIDERS.get(key)
    if row is None:
        raise AuthError(f"unknown provider {key!r} — available: " + ", ".join(provider_names()))
    if not row.installed():
        installed = installed_names()
        raise AuthError(
            f"provider {key!r} needs its own CLI ({row.binary}), which is not "
            "on PATH — installed providers: "
            + (", ".join(installed) if installed else "none")
        )
    return row


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_URL = re.compile(r"https://[^\s<>\x00-\x1f]+")
_CODE = re.compile(
    r"(?:device|one.time|verification)\s+code\s*[:=-]?\s*([A-Z0-9]{4,}(?:-[A-Z0-9]{3,})?)",
    re.IGNORECASE,
)
_AUTH_HOSTS = {
    "auth.openai.com", "chatgpt.com", "claude.ai", "console.anthropic.com",
    "platform.openai.com",
}


def public_progress(raw: str) -> str | None:
    """Extract only expected user-facing login data from a CLI output line."""
    line = _ANSI.sub("", raw).strip()
    for match in _URL.finditer(line):
        url = match.group().rstrip(".,)")
        parts = urlsplit(url)
        forbidden = {"access_token", "refresh_token", "id_token", "api_key", "code"}
        if (
            parts.hostname in _AUTH_HOSTS
            and parts.username is None
            and parts.password is None
            and not forbidden.intersection(parse_qs(parts.query))
        ):
            return f"Open in your browser: {url}"
    match = _CODE.search(line)
    if match:
        return f"Device code: {match.group(1)}"
    return None


async def run_auth_command(
    cmd: tuple[str, ...],
    progress: Callable[[str], Awaitable[None]],
    *,
    timeout: float = AUTH_TIMEOUT_SECONDS,
) -> int:
    """Run an interactive CLI on a private PTY while Textual stays responsive.

    The child inherits CODEX_HOME and CLAUDE_CONFIG_DIR from DOXA's process,
    so it authenticates the same CLI profile the user selected. The PTY
    keeps browser-login behavior that some CLIs disable for piped stdout.
    """
    master, slave = pty.openpty()
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=slave, stdout=slave, stderr=slave,
                env=os.environ.copy(), start_new_session=True,
            )
        except OSError:
            os.close(master)
            return 127
    finally:
        os.close(slave)
    pending = ""

    async def stop_child() -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    ready = await asyncio.to_thread(select.select, [master], [], [], 0.25)
                    if not ready[0]:
                        if proc.returncode is not None:
                            break
                        continue
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break  # Linux PTY EOF
                    raise
                if not chunk:
                    break
                pending += chunk.decode("utf-8", errors="replace")
                # CR is also a screen update separator in both CLIs.
                lines = re.split(r"[\r\n]", pending)
                pending = lines.pop()
                for line in lines:
                    visible = public_progress(line)
                    if visible:
                        await progress(visible)
                pending = pending[-4096:]
            if pending:
                visible = public_progress(pending)
                if visible:
                    await progress(visible)
            return await proc.wait()
    except TimeoutError:
        await stop_child()
        return 124
    except asyncio.CancelledError:
        await stop_child()
        raise
    finally:
        os.close(master)
