# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.auth -- ``/login`` and ``/logout``, delegated to the provider's own CLI.

DOXA never handles a credential. It suspends the TUI (Textual's
``App.suspend()``, the supported way to hand the terminal to another
program) and execs the provider's OWN interactive auth command, which owns
the browser handoff, the token storage and the keychain exactly as it does
when the user runs it by hand. When the child exits, DOXA resumes and
re-reads identity. Nothing is written to disk by DOXA on this path.

The provider table is DATA: each row is
``(login_cmd, logout_cmd, probe_cmd)`` plus a label, so supporting another
agent CLI is a row, not a code path. The commands below were PROBED against
the installed CLIs rather than assumed:

* ``claude`` 2.1.228 -- ``claude auth login`` / ``claude auth logout`` /
  ``claude auth status`` (from ``claude auth --help``; note it is the
  ``auth`` subcommand group, NOT a top-level ``claude login``).
* ``codex`` -- ``codex login`` / ``codex logout`` / ``codex login status``
  (from ``codex --help`` and ``codex login --help``; here the verbs ARE
  top-level, which is precisely why the table exists).

A provider whose CLI is not on PATH is an error that names the providers
that ARE installed -- never a silent no-op, never a guessed command.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class AuthError(RuntimeError):
    """Unknown provider, or a provider whose CLI is not installed."""


@dataclass(frozen=True)
class AuthProvider:
    """One row of the provider table."""

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
        name="claude",
        label="Claude (Anthropic)",
        login_cmd=("claude", "auth", "login"),
        logout_cmd=("claude", "auth", "logout"),
        probe_cmd=("claude", "auth", "status"),
    ),
    "codex": AuthProvider(
        name="codex",
        label="Codex (OpenAI)",
        login_cmd=("codex", "login"),
        logout_cmd=("codex", "logout"),
        probe_cmd=("codex", "login", "status"),
    ),
}

DEFAULT_PROVIDER = "claude"


def provider_names() -> list[str]:
    return list(PROVIDERS)


def installed_names() -> list[str]:
    return [name for name, row in PROVIDERS.items() if row.installed()]


def resolve(name: "str | None") -> AuthProvider:
    """Table lookup with an error that LISTS the alternatives -- the same
    never-guess posture peers.resolve_peer takes on an ambiguous prefix."""
    key = (name or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    row = PROVIDERS.get(key)
    if row is None:
        raise AuthError(
            f"unknown provider {key!r} — available: "
            + ", ".join(provider_names())
        )
    if not row.installed():
        installed = installed_names()
        raise AuthError(
            f"provider {key!r} needs its own CLI ({row.binary}), which is not "
            "on PATH — installed providers: "
            + (", ".join(installed) if installed else "none")
        )
    return row


def run_auth_command(cmd: "tuple[str, ...] | list[str]") -> int:
    """Run the provider's interactive auth command on the REAL terminal,
    stdio inherited (the child owns the prompt/browser handoff). Factored
    out as the single exec site so the TUI path can be tested without ever
    launching a browser. Returns the child's exit code; a missing binary
    (a race against the installed() check) comes back as 127, the shell's
    own convention, rather than an exception through the suspend block."""
    try:
        return subprocess.call(list(cmd))
    except OSError:
        return 127
