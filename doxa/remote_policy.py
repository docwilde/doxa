# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.remote_policy -- the authorization decision for a remote driver.

docs/plans/remote.md, "Authorization: the part to get right": a reachable
daemon socket is remote code execution with the user's own privileges, and
"no amount of UI care compensates for getting this wrong." This module is
track R1 of that spec -- the POLICY only. There is no socket, no TLS, no
HTTP header parsing here, and there will not be until a later track wires
a bridge process to it: this module's whole job is to be the thing that
bridge asks, so the answer is decided once, in one place that a security
review can read start to finish, rather than re-derived at each call site
the bridge grows.

Three independent questions, matching the spec's own numbered list:

    (a) :func:`remote_enabled` -- is a remote listener allowed to exist
        at all.
    (b) :func:`identity_decision` -- is THIS identity, having arrived on
        THIS listener, one DOXA trusts.
    (c) :func:`request_kind_decision` -- is THIS kind of request one the
        reduced remote surface grants at all.

Every decision comes back as a :class:`Decision` -- never a bare bool.
The spec's own words: "a request from a non-allow-listed identity is
refused, and the refusal is visible rather than silent." A bool cannot
carry a reason; a :class:`Decision` always does, and :func:`evaluate` is
the one function that composes the three into the single yes/no (plus
why) a bridge actually needs to act on.

**The owner's decisions this module implements verbatim, not
re-litigates:**

* Remote listening is OFF by default (:func:`remote_enabled`).
* The ``Tailscale-User-Login`` header is trusted only after the transport
  attests the local proxy. This module never touches the header itself;
  the bridge passes ``from_loopback`` as its historical boolean name only
  after checking its Unix peer credentials. A plain loopback TCP address
  does not pass that check and is refused regardless of the claimed login.
* DOXA keeps its OWN allow-list on top of the tailnet's -- defence in
  depth, :func:`allowed_logins`. An EMPTY allow-list refuses everyone; it
  does not fall back to permitting everyone, which is the direction every
  other allow-list bug in security software fails in.
* The remote surface is SMALLER than the local one. A remote driver may
  read the transcript/status, send prompts, and approve or deny a pending
  tool call. It cannot run ``!`` shell (:mod:`doxa.shell`'s own
  "only a keystroke reaches this" invariant) and cannot raise the
  permission mode to ``bypassPermissions``, unless a separate, explicit
  opt-in setting is on for each. See :func:`request_kind_decision`.

**Design note: pure decision functions, thin config readers.** Every
function whose name ends in ``_decision`` is a pure function of its
arguments -- no config file, no environment, no I/O -- the same split
:func:`doxa.engine.available_modes` already draws between "what is
armed" (an argument) and "what that arming permits" (a pure function of
it). This is what lets a test prove a refusal is not VACUOUS: a test can
call ``identity_decision(..., allow_list=frozenset())`` to prove the
empty-list refusal fires, and then call the SAME function with a
permissive ``allow_list`` to prove it is not hard-wired to always
refuse -- see ``tests/test_remote_policy.py``'s
"...against a permissive policy" tests. The config-reading functions
(:func:`remote_enabled`, :func:`allowed_logins`, :func:`remote_allow_shell`,
:func:`remote_allow_bypass`) are the thin, impure layer on top, each doing
nothing but reading one :mod:`doxa.config` row and handing the result to
the pure function beside it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config as config_mod

# -- request kinds -----------------------------------------------------
#
# "Request kinds at minimum" per the spec. Named as constants, not bare
# strings at each call site, for the same reason doxa.engine names its
# permission modes: a typo in a string literal is a silent no-op refusal
# (or worse, a silent grant) that nothing catches, where a typo'd
# constant name is a NameError at import time.

REQUEST_READ_TRANSCRIPT = "read_transcript"
REQUEST_READ_STATUS = "read_status"
REQUEST_SEND_PROMPT = "send_prompt"
REQUEST_APPROVE_TOOL = "approve_tool"
REQUEST_DENY_TOOL = "deny_tool"
REQUEST_SHELL_BANG = "shell_bang"
REQUEST_SET_PERMISSION_MODE = "set_permission_mode"

REQUEST_KINDS = (
    REQUEST_READ_TRANSCRIPT,
    REQUEST_READ_STATUS,
    REQUEST_SEND_PROMPT,
    REQUEST_APPROVE_TOOL,
    REQUEST_DENY_TOOL,
    REQUEST_SHELL_BANG,
    REQUEST_SET_PERMISSION_MODE,
)

# The reduced surface, split exactly the way docs/plans/remote.md draws
# it: everything a remote driver needs to watch a session and steer its
# ordinary turns, versus the two capabilities that turn a phone notification
# into either arbitrary code execution (shell) or an unattended session
# that stops asking (bypassPermissions). ALWAYS_PERMITTED_KINDS still runs
# through identity_decision and remote_enabled first -- being in this
# tuple means "not gated a second time on top of that", not "free for
# anyone".
ALWAYS_PERMITTED_KINDS = (
    REQUEST_READ_TRANSCRIPT,
    REQUEST_READ_STATUS,
    REQUEST_SEND_PROMPT,
    REQUEST_APPROVE_TOOL,
    REQUEST_DENY_TOOL,
)

# set_permission_mode is here too, but ONLY when its target is
# BYPASS_MODE_NAME -- see request_kind_decision. Every other target mode
# (default, acceptEdits, plan, auto, dontAsk) is on the reduced surface
# exactly like ALWAYS_PERMITTED_KINDS above; only the single most
# permissive mode is gated, matching the spec's own "bypassPermissions
# deserves an explicit decision" -- not a blanket refusal of the whole
# request kind.
GATED_KINDS = (REQUEST_SHELL_BANG, REQUEST_SET_PERMISSION_MODE)

# doxa.engine.BYPASS_MODE, restated rather than imported. doxa.engine
# pulls in the Claude Agent SDK, lore_core and the rest of a running
# session's dependency graph; this module has to stay importable by a
# bridge process (or a test) that has constructed none of that yet, and a
# policy decision does not need any of it to know the NAME of the one
# mode it treats specially. The two are asserted equal in
# tests/test_remote_policy.py so this cannot silently drift from the
# engine's own constant.
BYPASS_MODE_NAME = "bypassPermissions"


@dataclass(frozen=True)
class Decision:
    """One yes/no, with the reason a human should see.

    Never collapse this to a bare bool at a call site that shows anything
    to a user -- ``reason`` is the whole point: docs/plans/remote.md's
    testing bar requires that "a request from a non-allow-listed identity
    is refused, and the refusal is visible rather than silent," and a
    bool cannot carry the sentence that makes it visible."""

    allowed: bool
    reason: str

    @classmethod
    def allow(cls, reason: str) -> "Decision":
        return cls(allowed=True, reason=reason)

    @classmethod
    def refuse(cls, reason: str) -> "Decision":
        return cls(allowed=False, reason=reason)

    def __bool__(self) -> bool:
        # Convenience for `if decision:`, but callers that SHOW anything
        # to a user must still read .reason -- see the class docstring.
        return self.allowed


# -- (a) is a remote listener allowed to exist at all -------------------


def remote_listening_decision(*, enabled: bool) -> Decision:
    """Pure form of :func:`remote_enabled`'s answer, as a Decision."""
    if enabled:
        return Decision.allow(
            "remote listening is enabled (remote_enabled / DOXA_REMOTE_ENABLED)"
        )
    return Decision.refuse(
        "remote listening is OFF -- this is the default, and nothing "
        "turned it on (remote_enabled / DOXA_REMOTE_ENABLED)"
    )


def remote_enabled() -> bool:
    """``DOXA_REMOTE_ENABLED`` / the config file's ``remote_enabled`` row.

    OFF by default -- loopback-only stays the behaviour unless this is
    explicitly turned on, which is the whole substance of "remote
    listening is OFF by default" rather than a conservative habit."""
    raw = config_mod.raw("DOXA_REMOTE_ENABLED").strip()
    return bool(raw) and raw.lower() not in ("0", "false", "no", "off")


# -- (b) is this identity, on this listener, one DOXA trusts ------------


def identity_decision(
    login: "str | None",
    *,
    from_loopback: bool,
    allow_list: "frozenset[str]",
) -> Decision:
    """Pure form of "is this identity allowed to drive this session".

    ``from_loopback`` is the historical name of the load-bearing
    transport-attestation boolean. The peer bridge sets it only after
    Linux ``SO_PEERCRED`` identifies a Unix-socket client as tailscaled;
    plain loopback TCP is false because any local process can forge an
    HTTP header. When false, this function refuses before looking at
    ``login`` or ``allow_list``.

    ``allow_list`` empty refuses EVERY login, including a real,
    loopback-verified one -- the defence-in-depth rule: DOXA's own list
    is a second gate, not a rubber stamp on the tailnet's."""
    if not from_loopback:
        return Decision.refuse(
            "identity header ignored: it did not arrive on the loopback "
            "listener tailscale serve forwards to -- the "
            "Tailscale-User-Login header is trusted on no other path"
        )
    normalized = (login or "").strip()
    if not normalized:
        return Decision.refuse("no identity presented")
    if not allow_list:
        return Decision.refuse(
            "the remote allow-list is empty -- an empty allow-list "
            "refuses everyone, it does not grant access to everyone"
        )
    if normalized.casefold() not in allow_list:
        return Decision.refuse(
            f"{normalized!r} is not on the remote allow-list "
            "(remote_allowed_logins / DOXA_REMOTE_ALLOWED_LOGINS)"
        )
    return Decision.allow(f"{normalized!r} is on the remote allow-list")


def allowed_logins() -> "frozenset[str]":
    """``DOXA_REMOTE_ALLOWED_LOGINS`` / the config file's
    ``remote_allowed_logins`` row, parsed into a casefolded set.

    Comma-separated, same shape as the colleague's own
    ``TELAG_ALLOWED_TS_USERS`` (docs/plans/remote.md). Casefolded because
    a Tailscale login is practically an email address and a user should
    not be silently locked out by a capitalization difference between
    what they typed into config.toml and what the tailnet reports --
    :func:`identity_decision` casefolds the incoming login to match.
    Empty entries (a stray comma, leading/trailing whitespace) are
    dropped rather than becoming a login nothing can ever equal."""
    raw = config_mod.raw("DOXA_REMOTE_ALLOWED_LOGINS")
    return frozenset(
        entry.strip().casefold() for entry in raw.split(",") if entry.strip()
    )


# -- (c) is this request kind granted on the reduced remote surface -----


def request_kind_decision(
    kind: str,
    *,
    shell_opt_in: bool,
    bypass_opt_in: bool,
    target_mode: "str | None" = None,
) -> Decision:
    """Pure form of "is this KIND of request one the remote surface
    grants" -- deliberately blind to identity and to whether remote
    listening is even on; :func:`evaluate` composes those in. Keeping
    this function narrow to just the kind/opt-in question is what lets a
    test exercise "shell is refused without the opt-in, permitted with
    it" without also having to stand up an identity or a config file.

    ``target_mode`` only matters for
    :data:`REQUEST_SET_PERMISSION_MODE`: the spec gates
    ``bypassPermissions`` specifically, not permission-mode changes in
    general -- a remote driver may still set ``acceptEdits`` or ``plan``
    with no opt-in at all. A ``set_permission_mode`` request with no
    ``target_mode`` given is refused rather than guessed at: this
    function fails CLOSED on missing information, the same direction an
    empty allow-list fails in."""
    if kind not in REQUEST_KINDS:
        return Decision.refuse(f"{kind!r} is not a recognized request kind")
    if kind == REQUEST_SHELL_BANG:
        if shell_opt_in:
            return Decision.allow("shell_bang is permitted -- remote_allow_shell is on")
        return Decision.refuse(
            "shell_bang is refused on the remote surface by default "
            "(remote_allow_shell / DOXA_REMOTE_ALLOW_SHELL is off) -- "
            "doxa.shell is reachable only from a keystroke typed into "
            "this window"
        )
    if kind == REQUEST_SET_PERMISSION_MODE:
        if not target_mode:
            return Decision.refuse(
                "set_permission_mode with no target mode given is refused"
            )
        if target_mode != BYPASS_MODE_NAME:
            return Decision.allow(
                f"set_permission_mode({target_mode!r}) is permitted -- "
                "only bypassPermissions is gated on the remote surface"
            )
        if bypass_opt_in:
            return Decision.allow(
                "set_permission_mode(bypassPermissions) is permitted -- "
                "remote_allow_bypass is on (doxa.engine's own launch-time "
                "arming check still applies on top of this)"
            )
        return Decision.refuse(
            "set_permission_mode(bypassPermissions) is refused on the "
            "remote surface by default (remote_allow_bypass / "
            "DOXA_REMOTE_ALLOW_BYPASS is off) -- a mode that stops "
            "asking, requested from a phone that might be unlocked on a "
            "table, is a different risk than the same mode requested at "
            "the keyboard"
        )
    # Every other kind is on ALWAYS_PERMITTED_KINDS -- gated only by
    # remote_enabled and identity, both handled by evaluate(), not here.
    return Decision.allow(f"{kind} is on the reduced remote surface")


def remote_allow_shell() -> bool:
    """``DOXA_REMOTE_ALLOW_SHELL`` / ``remote_allow_shell``. OFF by
    default -- see :func:`request_kind_decision`'s shell_bang branch for
    what turning it on actually grants."""
    raw = config_mod.raw("DOXA_REMOTE_ALLOW_SHELL").strip()
    return bool(raw) and raw.lower() not in ("0", "false", "no", "off")


def remote_allow_bypass() -> bool:
    """``DOXA_REMOTE_ALLOW_BYPASS`` / ``remote_allow_bypass``. OFF by
    default, and independent of :func:`doxa.engine.bypass_arming_enabled`
    -- that one arms THIS session's CLI to reach bypassPermissions at
    all; this one decides whether a request arriving over the network may
    ask for it. Both must be true for a remote bypass request to
    succeed."""
    raw = config_mod.raw("DOXA_REMOTE_ALLOW_BYPASS").strip()
    return bool(raw) and raw.lower() not in ("0", "false", "no", "off")


# -- composing all three into the one answer a bridge acts on -----------


def evaluate(
    kind: str,
    *,
    login: "str | None",
    from_loopback: bool,
    target_mode: "str | None" = None,
) -> Decision:
    """The single entry point a future bridge process calls: is THIS
    request, of THIS kind, from THIS identity arriving on THIS listener,
    granted right now.

    Composes the three pure decisions in the order that matters for a
    clear refusal reason -- remote listening, then identity, then the
    kind-specific gate -- and returns the FIRST refusal rather than every
    one that would apply, because a bridge showing a denial needs one
    sentence, not a list. Every sub-decision is still independently
    reachable (:func:`remote_listening_decision`,
    :func:`identity_decision`, :func:`request_kind_decision`) for a test,
    or a future audit log, that wants to see all three."""
    listening = remote_listening_decision(enabled=remote_enabled())
    if not listening.allowed:
        return listening
    identity = identity_decision(
        login,
        from_loopback=from_loopback,
        allow_list=allowed_logins(),
    )
    if not identity.allowed:
        return identity
    return request_kind_decision(
        kind,
        shell_opt_in=remote_allow_shell(),
        bypass_opt_in=remote_allow_bypass(),
        target_mode=target_mode,
    )
