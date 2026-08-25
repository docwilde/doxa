"""doxa.providers -- model-catalog seam: ONE Protocol between the UI (the
model picker, doxa/session/chips.py) and however a provider's catalog gets
resolved, so a second provider (DeepSeek, Codex -- the vault addendum 6 multi-
provider engines) is a new Protocol implementation later, never a UI
change now. This module does ONLY model listing -- nothing about running
turns, spawning engines, or anything else the name might tempt it to grow
into; if the seam wants more than that, it should stop here and grow a
second module instead.

MODEL LIST SOURCE (queue item Y / status-chips): resolution order, most
authoritative first --

1. The Anthropic Models API (``client.models.list()``). VERIFIED
   EMPIRICALLY unreachable under DOXA's normal auth posture: DOXA
   authenticates through the ``claude`` CLI's own OAuth session
   (doxa/auth.py's own docstring -- "DOXA never handles a credential") and
   deliberately never reads that token out of the CLI's keychain/config --
   the same posture that rejected ``--bare``'s forced ``ANTHROPIC_API_KEY``
   auth in doxa/cli_isolation.py (measured there to silently log an
   authenticated user OUT). A live probe against this exact class
   (``anthropic.Anthropic()`` with no key configured, run from this repo's
   own venv, no other env changes) fails at CLIENT CONSTRUCTION, before
   any network call:
   ``TypeError: Could not resolve authentication method. Expected one of
   api_key, auth_token, or credentials to be set. Or for one of the
   `X-Api-Key` or `Authorization` headers to be explicitly omitted`` --
   so this tier is written defensively (guarded import, guarded API-key
   presence check, guarded call) and used OPPORTUNISTICALLY: if the
   operator's own shell happens to export ``ANTHROPIC_API_KEY`` (DOXA's
   own process env is untouched by cli_isolation.py, which isolates only
   the SPAWNED engine subprocess's env -- see that module's docstring),
   this tier fires for real; on the documented OAuth-only posture it is
   skipped without ever attempting the call, and the picker says so (see
   ``ModelInfo.source`` / the picker's "static fallback" note).
2. Whatever the installed ``claude_agent_sdk`` package advertises. CHECKED
   (this repo's own venv, the pinned ``claude-agent-sdk``): no MODEL/
   MODELS constant anywhere in ``types.py`` / ``__init__.py`` / the client
   module, and ``ClaudeSDKClient.set_model`` accepts an arbitrary string
   with no enumerated catalog behind it. This tier is a structural no-op
   TODAY -- kept as its own method (never folded into the fallback) so the
   resolution order in code matches the order in this docstring exactly,
   and a future SDK release that DOES advertise a catalog only has to fill
   in one method body.
3. A small STATIC fallback, clearly marked as such (``ModelInfo.source ==
   "fallback"``) -- the same four aliases ``doxa.ui.labels.MODEL_ALIASES``
   already used before this feature (``haiku``, ``sonnet``, ``opus``,
   ``fable``), sourced from the installed ``claude`` CLI's own ``--model``
   help text ("provide an alias for the latest model (e.g. 'fable',
   'opus', or 'sonnet')"). ``doxa.ui.labels`` now imports THIS tuple rather
   than keeping a second copy -- one list, not two that happen to agree
   today.

NOT a dependency: ``anthropic`` is intentionally absent from pyproject.toml
-- it is imported lazily, inside a ``try``, only when an API key is
already present. Adding it as a hard dependency would pull real weight
into every install for a tier that is structurally unreachable for DOXA's
primary (subscription/OAuth) audience; an operator who genuinely wants
tier 1 live can ``pip install anthropic`` into this venv themselves.

Cached on the instance for the life of one ``ClaudeProvider`` (one built
per ``SessionPane``, in its ``__init__``) -- the picker opens on every
click and must never re-probe the network, or re-run the same guarded-away
skip, each time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


# The CLI's own documented `--model` aliases (`claude --help`: "provide an
# alias for the latest model (e.g. 'fable', 'opus', or 'sonnet')") -- the
# static-fallback tier, and also doxa.ui.labels.MODEL_ALIASES's one source
# (see that name's own comment).
FALLBACK_MODEL_ALIASES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


@dataclass(frozen=True)
class ModelInfo:
    """One selectable model, as the picker shows it.

    ``id`` is what actually gets handed to `/model` / `engine.set_model`
    -- an alias from the fallback tier, or the API's own canonical model
    id when that tier is live. ``source`` is which resolution tier
    produced this entry ("api" or "fallback") -- the same value for every
    entry in one ``list_models()`` call, carried per-entry only so the
    caller doesn't need a second return channel to ask "which tier was
    this?"."""

    id: str
    display_name: str
    source: str


class ModelProvider(Protocol):
    """What the model picker needs from a provider -- listing only. A
    second provider (DeepSeek, Codex) is a new class satisfying this
    Protocol, never a branch inside the picker's own code.

    ASSESSED against docs/plans/plugin-api.md's fourth extension point (v0.34.0)
    and found to be the right shape for HALF of it. The catalog half is
    complete: the picker asks a provider what it can offer and never
    branches on who the provider is. The SESSION half is not here at all
    -- spawn, send, interrupt and the event stream are what
    :class:`doxa.engine.SessionEngine` and :class:`doxa.client.EngineClient`
    already agree on informally, by both exposing the same async-iterator
    surface, and there is no Protocol naming it. That is a second Protocol
    (this module's own docstring says it should stop at listing and grow a
    second module rather than swell), and writing it is feature work for
    the multi-provider engines, not something a refactor gets to invent."""

    def provider_display_name(self) -> str:
        ...

    def default_model(self) -> "str | None":
        ...

    async def list_models(self) -> list[ModelInfo]:
        ...


class ClaudeProvider:
    """The only provider DOXA drives today -- see
    doxa.ui.labels.PROVIDER_GLYPHS' own one-row comment for the parallel
    note on the tab-label side."""

    def __init__(self) -> None:
        self._cache: "list[ModelInfo] | None" = None

    def provider_display_name(self) -> str:
        return "Claude (Anthropic)"

    def default_model(self) -> "str | None":
        return None  # "default": whatever the CLI's own --model default is

    async def list_models(self) -> list[ModelInfo]:
        if self._cache is not None:
            return self._cache
        models = await self._try_api()
        if models is None:
            models = self._try_sdk_catalog()
        if models is None:
            models = [
                ModelInfo(id=alias, display_name=alias, source="fallback")
                for alias in FALLBACK_MODEL_ALIASES
            ]
        self._cache = models
        return models

    async def _try_api(self) -> "list[ModelInfo] | None":
        """Tier 1 -- see the module docstring for the empirical finding.
        Only even ATTEMPTED when an API key is actually present in DOXA's
        own process env (never the spawned engine's isolated one --
        cli_isolation.py's CLAUDE_CONFIG_DIR redirection has no bearing
        here). DOXA's documented OAuth posture has none, and constructing
        the client without one raises before any network call, so this
        stays a cheap, silent skip rather than a guaranteed failed round
        trip on every picker open."""
        if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            return None
        try:
            import anthropic  # optional -- see module docstring
        except ImportError:
            return None
        import asyncio

        def _fetch() -> "list[ModelInfo] | None":
            try:
                client = anthropic.Anthropic()
                page = client.models.list()
                return [
                    ModelInfo(
                        id=m.id,
                        display_name=str(getattr(m, "display_name", None) or m.id),
                        source="api",
                    )
                    for m in page
                ]
            except Exception:  # noqa: BLE001 -- any failure here means
                # "unreachable this way", never a crash; the fallback tier
                # picks it up.
                return None

        return await asyncio.to_thread(_fetch)

    def _try_sdk_catalog(self) -> "list[ModelInfo] | None":
        """Tier 2 -- see the module docstring: checked, currently always
        None."""
        return None
