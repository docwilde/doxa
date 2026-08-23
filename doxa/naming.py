"""doxa.naming -- ONE namer for a session, two consumers.

A session outside a git repo has no repo/branch to label its tab with, and
a session in LORE's index may carry no title at all -- both then fall back
to something the user cannot read (a directory name that says nothing, or a
bare hex id). This module turns the session's own first turn into two to
four words, once, and caches the answer so the tab strip and the ``/search``
result list show the SAME name for the same session.

Discipline, in the order it matters:

* **One call, cheap model, never blocking.** Haiku, one headless
  ``claude -p`` (the same call shape LORE's deriver uses), short timeout,
  run off the event loop. A failure is final for that session: the caller
  keeps its dirname fallback and NOTHING retries in a loop.
* **The prompt is a suggestion; the sanitizer is the guarantee.** The first
  turn is arbitrary user text, so whatever comes back is untrusted string
  data headed for a UI widget: control characters stripped, whitespace
  collapsed, punctuation reduced to spaces and hyphens, hard-truncated to
  :data:`NAME_MAX`. Asking for 2-4 words does not make the answer 2-4
  words.
* **Cached, and the cache is the persistence.** ``$DOXA_HOME/names.toml``
  maps session id -> name, so a restart (item D's window restore) reuses
  the name rather than spending a second call on the same session.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from . import cli_isolation as cli_isolation_mod
from . import config as config_mod

# Cheap and fast: naming a tab is not a reasoning task.
NAMER_MODEL = "claude-haiku-4-5"
NAMER_TIMEOUT_SECS = 20.0

# 2-4 words, ~24 columns: the label has to fit beside `Opus@`.
NAME_MAX = 24

_PROMPT = (
    "Name this coding session in 2 to 4 words, as a short title someone "
    "could recognise it by later. Reply with the title ONLY -- no quotes, "
    "no punctuation beyond spaces and hyphens, no explanation.\n\n"
    "First message of the session:\n"
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ALLOWED = re.compile(r"[^0-9A-Za-z \-]")


def sanitize(text: str, limit: int = NAME_MAX) -> str:
    """Model output -> something safe and short enough to put in a widget.

    Applied to EVERY name regardless of how well the prompt behaved: a
    model that returns three paragraphs, an ANSI escape or a newline gets
    the same treatment as one that answers correctly."""
    cleaned = _CONTROL.sub(" ", text or "")
    cleaned = _ALLOWED.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


def names_path() -> Path:
    return config_mod.doxa_home() / "names.toml"


def load_names() -> dict[str, str]:
    """The cache, or ``{}``. Never raises: a corrupt cache costs a name,
    never a session."""
    try:
        with names_path().open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return {}
    return {str(k): str(v) for k, v in names.items() if v}


def cached_name(session_id: str) -> "str | None":
    return load_names().get(str(session_id or "")) or None


def remember_name(session_id: str, name: str) -> None:
    """Write one mapping. Atomic (tmp + replace) and 0600, same discipline
    as the settings file -- and last-write-wins between tabs, which is
    correct: they are writing the same name for the same session."""
    session_id, name = str(session_id or ""), sanitize(name)
    if not session_id or not name:
        return
    names = load_names()
    if names.get(session_id) == name:
        return
    names[session_id] = name
    path = names_path()
    lines = [
        "# DOXA session names (generated once per session, then reused).",
        "# Safe to edit or delete; a missing name is regenerated on demand.",
        "",
        "[names]",
    ]
    for key in sorted(names):
        escaped = names[key].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"{key}" = "{escaped}"')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        return


def generate_name(first_message: str, model: str = NAMER_MODEL) -> "str | None":
    """One headless Haiku call. Returns a sanitized name, or None on any
    failure at all -- a missing binary, a non-zero exit, a timeout, an
    empty answer. None means "keep the fallback", and the caller must never
    turn it into a retry."""
    text = " ".join((first_message or "").split())[:2000]
    if not text:
        return None
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--allowedTools", ""],
            input=_PROMPT + text,
            capture_output=True,
            text=True,
            timeout=NAMER_TIMEOUT_SECS,
            # Same containment as the interactive engine (item AA,
            # doxa.cli_isolation): this is a spawned `claude` CLI too, so
            # it gets the same isolated config dir rather than DOXA's own
            # process env / ~/.claude -- LORE_SKIP=1 rides along the same
            # way it always has here.
            env={**os.environ, **cli_isolation_mod.spawn_env()},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return sanitize(proc.stdout) or None


def name_for(session_id: str, first_message: str) -> "str | None":
    """The one entry point both consumers call: cached name if there is
    one, otherwise one generation, cached on success.

    Blocking by design (a subprocess and a network round trip) -- callers
    run it off the event loop, exactly like the git subprocess in
    ``GitLine``'s constructor."""
    existing = cached_name(session_id)
    if existing:
        return existing
    name = generate_name(first_message)
    if name:
        remember_name(session_id, name)
    return name
