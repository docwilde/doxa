# SPDX-License-Identifier: AGPL-3.0-only
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

Cached on the instance for the life of one provider (one built per
``SessionPane`` per ENGINE, on first use) -- the picker opens on every
click and must never re-probe the network, or re-run the same guarded-away
skip, each time.

A SECOND AND THIRD PROVIDER (v1.12.0). ``ModelProvider``'s own docstring
below said a second provider "is a new class satisfying this Protocol,
never a branch inside the picker's own code"; :class:`VendorModelProvider`
is that class, and it serves BOTH chat-completions vendors from one
:class:`doxa.vendors.VendorSpec` for the same reason ``ChatApiEngine`` is
one class for both -- there is no second implementation for the two arms
of docs/plans/emergent-organization.md to drift into. Its resolution order
is the mirror of ``ClaudeProvider``'s and inverted in outcome, because the
measurement came out the other way: tier 1 (the vendor's own ``GET
/models``) is REACHABLE here -- both vendors are API-key authenticated,
the key is already in the environment for the session to run at all, and
the call is one small GET -- so it is tried first and the static
``VendorSpec.models`` tuple is the floor behind it, marked ``source ==
"fallback"`` exactly as Claude's aliases are.

WHY THE LIVE LIST MATTERS MORE HERE THAN IT DOES FOR CLAUDE. Measured
2026-09-17 while the vendor engines were built: DeepSeek answers a request
for ``deepseek-chat`` -- the name every older integration uses -- with
``deepseek-flash``, silently, and only the response says so. Its live
catalogue is two entries (``deepseek-flash``, ``deepseek-v4-pro``) and
neither ``deepseek-chat`` nor ``deepseek-reasoner`` is among them. A
picker offering a model that does not exist, or one that quietly becomes a
different model, is worse than a picker offering fewer: the operator picks
it, the session runs, and the transcript is attributed to a model that
never answered. So neither tier here ever contains a name DOXA made up --
tier 1 is the vendor's own answer, and tier 2 is the tuple that was read
off tier 1 on the date in ``VendorSpec.models``' comment.

WHICH ENGINE GETS WHICH CATALOGUE is :func:`model_provider`, keyed on the
ENGINE ids ``doxa.engines`` registers rather than on provider ids, so the
question "does every engine DOXA offers have a catalogue?" has one place
to be asked and one place to be answered -- including the answer "no, and
here is why", which is :data:`CATALOG_EXEMPT_ENGINES`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .engines import (
    CLAUDE_ENGINE_ID,
    CODEX_ENGINE_ID,
    DEEPSEEK_ENGINE_ID,
    GLM_ENGINE_ID,
    engine_id_of,
)

if TYPE_CHECKING:  # the spec is a doxa.vendors type, and importing that
    # module here would cost every launch the ~200 ms of lore_core it
    # pulls in -- for a class nothing constructs until a picker opens on a
    # vendor session. Resolved at the point of use instead (see
    # _vendor_catalog), the same laziness doxa.engines._LAZY_PROVIDERS
    # applies to the engine half.
    from .vendors import VendorSpec


# The CLI's own documented `--model` aliases (`claude --help`: "provide an
# alias for the latest model (e.g. 'fable', 'opus', or 'sonnet')") -- the
# static-fallback tier, and also doxa.ui.labels.MODEL_ALIASES's one source
# (see that name's own comment).
FALLBACK_MODEL_ALIASES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


# The short provider id for the provider DOXA was built on. ONE string,
# three readers: ``ClaudeProvider.provider_id()`` returns it,
# ``doxa.ui.labels.PROVIDER_GLYPHS`` keys on it, and
# ``doxa.engine.SessionEngine`` publishes it as ``PeerInfo.provider`` at
# connect (docs/plans/peer-publishing.md). Defined HERE, in the module that
# owns provider identity, rather than as a literal at each of the three --
# the peer-publishing spec's "no new vocabulary, two reused ones" is only
# true if the vocabulary has a place to live.
CLAUDE_PROVIDER_ID = "claude"

# The second provider id, and the first time this constant has had a
# sibling. Same three readers as CLAUDE_PROVIDER_ID above -- the engine
# publishes it as ``PeerInfo.provider``, ``doxa.ui.labels.PROVIDER_GLYPHS``
# keys on it, and it names the vendor rather than the CLI (the CLI is
# ``doxa.engines.CODEX_ENGINE_ID``; a provider and an engine are different
# questions, which is precisely why PeerInfo carries both fields).
#
# No ``CodexProvider(ModelProvider)`` ships beside it: this module lists
# MODELS, and enumerating Codex's catalog is a second measurement nobody
# has made. The picker keeps saying what it can source; it does not learn
# to guess for a second vendor.
CODEX_PROVIDER_ID = "openai"

# The third and fourth provider ids, the two chat-completions vendors.
# Same three readers as the two above, and defined HERE rather than as
# literals on the VendorSpec rows in doxa.vendors for the reason
# CLAUDE_PROVIDER_ID states: provider identity has one place to live, and
# a spec that spelled its own would be a second copy of a string the peer
# rail and the glyph table also key on.
DEEPSEEK_PROVIDER_ID = "deepseek"
ZAI_PROVIDER_ID = "zai"


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

    def provider_id(self) -> str:
        """The short, machine-facing id -- ``"claude"``, the same key
        ``doxa.ui.labels.PROVIDER_GLYPHS`` uses and the same string a
        session publishes as ``PeerInfo.provider``. Distinct from
        :meth:`provider_display_name`, which is prose for a picker header
        ("Claude (Anthropic)") and would be a poor registry value."""
        ...

    def provider_display_name(self) -> str:
        ...

    def default_model(self) -> "str | None":
        ...

    async def list_models(self) -> list[ModelInfo]:
        ...

    def catalog_note(self, models: list[ModelInfo]) -> str:
        """One line naming WHICH tier produced ``models``, or ``""`` when
        the authoritative one did and there is nothing to caveat.

        On the Protocol rather than in the picker, and a pure function of
        what was resolved rather than a flag the provider remembers: the
        picker must render a provider's caveat without knowing whose it is
        (this module's rule -- "never a branch inside the picker's own
        code"), and the caveats genuinely differ. Claude's fallback means
        an OAuth posture that cannot reach the Models API; a vendor's means
        its ``GET /models`` was not answered, which is a different fact and
        deserves different words."""
        ...


class ClaudeProvider:
    """The provider DOXA was built on, and the one whose catalogue is
    hardest to get at -- see the module docstring's tier 1 for the
    empirical finding, and doxa.ui.labels.PROVIDER_GLYPHS' own one-row
    comment for the parallel note on the tab-label side."""

    def __init__(self) -> None:
        self._cache: "list[ModelInfo] | None" = None

    def provider_id(self) -> str:
        return CLAUDE_PROVIDER_ID

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

    def catalog_note(self, models: list[ModelInfo]) -> str:
        if models and models[0].source == "fallback":
            return (
                "model catalog: static fallback -- the Anthropic Models "
                "API is not reachable under this session's OAuth auth"
            )
        return ""

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


class VendorModelProvider:
    """The catalogue half for one chat-completions vendor.

    ONE class for both, built from a :class:`doxa.vendors.VendorSpec`, for
    the same reason ``ChatApiEngine`` is one class for both: everything
    that differs between DeepSeek and GLM is already a field on the spec,
    and a second implementation would be a place for the two arms of the
    capability-parity experiment to drift apart.

    NO CREDENTIAL IS HELD. The key is read from the environment at the
    moment a request is built and dropped when it returns -- the guarantee
    ``ChatApiEngine`` states and this class repeats verbatim, because a
    provider cached for the life of a pane is exactly the object a
    stored key would outlive its usefulness on. There is no key attribute
    here for a ``repr``, a ``vars()`` or a pickle to find.

    ``fetch`` is injectable for the reason ``transport`` is: the suite
    proves the live shape, the empty answer and the no-key path with no
    network and no credential."""

    def __init__(
        self,
        spec: "VendorSpec",
        fetch: "Callable[..., tuple[str, ...]] | None" = None,
    ) -> None:
        self._spec = spec
        self._fetch = fetch
        self._cache: "list[ModelInfo] | None" = None

    def provider_id(self) -> str:
        return self._spec.provider_id

    def provider_display_name(self) -> str:
        return self._spec.display_name

    def default_model(self) -> "str | None":
        """The spec's own default -- NOT None the way Claude's is. A
        vendor request carries an explicit ``model`` field with no
        server-side default behind it, so "whatever the CLI would pick" has
        no meaning here and the picker shows the name a new session on this
        vendor actually starts with."""
        return self._spec.default_model

    async def list_models(self) -> list[ModelInfo]:
        if self._cache is not None:
            return self._cache
        models = await self._try_api()
        if models is None:
            models = [
                ModelInfo(id=name, display_name=name, source="fallback")
                for name in self._spec.models
            ]
        self._cache = models
        return models

    def catalog_note(self, models: list[ModelInfo]) -> str:
        if models and models[0].source == "fallback":
            return (
                f"model catalog: static fallback -- "
                f"{self._spec.display_name} did not answer GET /models "
                f"(${self._spec.env_var} unset, or the call failed), so "
                f"this is the list measured when the engine was built and "
                f"the vendor may have moved on"
            )
        return ""

    async def _try_api(self) -> "list[ModelInfo] | None":
        """Tier 1 -- the vendor's own ``GET /models``.

        Skipped without ever attempting the call when the key is absent
        (``MissingCredential``), which is the same cheap guarded skip
        ``ClaudeProvider._try_api`` makes for ``ANTHROPIC_API_KEY`` -- a
        session that cannot authenticate cannot run a turn either, and the
        picker should say what it can source rather than stall on a 401.

        Runs the blocking ``urllib`` call on a worker thread. An empty
        answer -- unreachable, refused, or a catalogue with nothing in it
        -- returns None so :meth:`list_models` falls to the static tier,
        because a picker with no rows tells an operator nothing they can
        act on."""
        from . import vendors as vendors_mod

        try:
            api_key = vendors_mod.credential(self._spec)
        except vendors_mod.MissingCredential:
            return None
        fetch = self._fetch or vendors_mod.fetch_models
        names = await asyncio.to_thread(fetch, self._spec, api_key)
        del api_key
        if not names:
            return None
        return [
            ModelInfo(id=name, display_name=name, source="api") for name in names
        ]


# -- which engine has which catalogue ----------------------------------
#
# Keyed on ENGINE ids (doxa.engines' registry), not provider ids: "can the
# picker list models for the engine this session is running?" is the
# question, and an engine is what a session is started with. The closure
# test in tests/test_engine_picker.py walks engines.available() against
# this mapping plus the exemption set below, so a fifth engine arrives
# either with a catalogue or with a written reason -- never with the
# previous engine's catalogue, which is what the picker did for every
# non-Claude session before this existed.


#: Engines that deliberately publish no catalogue, and why.
#:
#: ``codex``: the reason doxa.providers has carried since v1.4.0 in
#: CODEX_PROVIDER_ID's own comment -- this module lists MODELS, and
#: enumerating the Codex CLI's catalogue is a measurement nobody has made.
#: ``codex exec --model`` takes an arbitrary string with nothing
#: enumerated behind it, exactly as ``ClaudeSDKClient.set_model`` does, and
#: the four Claude aliases are not Codex's. So the picker says so and
#: ``/model <id>`` still works -- the CLI remains the authority, and DOXA
#: does not guess on its behalf.
CATALOG_EXEMPT_ENGINES: "frozenset[str]" = frozenset({CODEX_ENGINE_ID})


def no_catalog_text(engine_id: str) -> str:
    """What a surface says instead of listing models for an engine in
    :data:`CATALOG_EXEMPT_ENGINES`.

    Written once, here, beside the exemption itself: the sentence and the
    reason for it are the same fact, and a caller that composed its own
    would be free to say something the exemption does not mean."""
    return (
        f"model: the {engine_id} engine publishes no model catalogue -- its "
        "CLI takes an arbitrary --model string with nothing enumerated "
        "behind it, so DOXA lists nothing rather than guessing. `/model "
        "<id>` still sets one."
    )


def _vendor_catalog(engine_id: str) -> "ModelProvider":
    """One vendor's catalogue provider, resolving its spec at the point of
    use -- see the TYPE_CHECKING note at the top of this module for why
    ``doxa.vendors`` is not imported at module scope."""
    from . import vendors as vendors_mod

    return VendorModelProvider(vendors_mod.VENDORS[engine_id])


#: engine id -> how to build its catalogue provider. A dict of FACTORIES,
#: not instances: ``list_models()`` caches on the instance, and whoever
#: holds the instance owns how long that cache lives (a SessionPane holds
#: one per engine for its own life). A module-level instance would make
#: that cache process-wide and outlive the test that filled it.
_CATALOG_BUILDERS: "dict[str, Callable[[], ModelProvider]]" = {
    CLAUDE_ENGINE_ID: ClaudeProvider,
    DEEPSEEK_ENGINE_ID: lambda: _vendor_catalog(DEEPSEEK_ENGINE_ID),
    GLM_ENGINE_ID: lambda: _vendor_catalog(GLM_ENGINE_ID),
}


def model_provider(engine_id: "str | None") -> "ModelProvider | None":
    """A fresh catalogue provider for one ENGINE id, or ``None``.

    ``None`` means this engine publishes no catalogue -- see
    :data:`CATALOG_EXEMPT_ENGINES` -- and every caller says so rather than
    falling back to another engine's list, which is the exact defect this
    function exists to close: before it, every session opened the model
    chip onto Claude's four aliases, including a DeepSeek one, where
    picking ``sonnet`` would have been sent to DeepSeek verbatim.

    An UNKNOWN id is also ``None``, deliberately without raising: unlike
    :func:`doxa.engines.get`, which decides whether a session starts at
    all, this decides whether a picker has rows, and a handle that names
    an engine this build has never heard of is a reason to show nothing,
    not to break a repaint."""
    key = (engine_id or "").strip().lower() or CLAUDE_ENGINE_ID
    builder = _CATALOG_BUILDERS.get(key)
    return builder() if builder is not None else None


def provider_for(engine: Any) -> "ModelProvider | None":
    """The catalogue provider for a live engine handle.

    The composition of :func:`doxa.engines.engine_id_of` and
    :func:`model_provider`, so a call site holding a handle never has to
    spell the duck-typed attribute itself."""
    return model_provider(engine_id_of(engine))
