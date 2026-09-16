# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.remote_policy (R1, docs/plans/remote.md): the authorization
decision for a remote driver -- track R1's own "Testing bar" section,
verbatim:

    - a remote listener is ABSENT unless explicitly enabled -- assert on
      the default, since a default is what almost everyone runs
    - a request from a non-allow-listed identity is refused, and the
      refusal is VISIBLE rather than silent
    - the reduced remote surface actually refuses what it claims to
      refuse (the `!`-shell and `bypassPermissions` cases, written as
      security assertions the way v0.36.0's "the model cannot reach the
      shell" test is written -- including its lesson that such a test
      passes VACUOUSLY until the capability exists, so it must be
      verified against a deliberately unsafe build)

That last lesson is why almost every refusal test below has a companion
that flips ONE input -- the allow-list, `from_loopback`, an opt-in flag --
and asserts the SAME function now allows. A refusal test with no such
companion proves nothing: it would pass identically if
``request_kind_decision`` were ``def request_kind_decision(*a, **k): return
Decision.refuse("no")`` and the security boundary did not exist at all.
The companion is what tests/test_shell.py's own
``test_the_shell_executor_is_on_no_tool_surface_the_model_can_call`` calls
"this test would pass vacuously" -- guarded here the same way, by proving
the refusing branch is reachable-but-not-taken rather than absent.

No transport is exercised here (there is none yet -- see doxa/remote_policy
.py's own docstring): every test drives the pure decision functions
directly, or the thin config readers layered on doxa.config, exactly the
seam a bridge process will use once one exists.
"""

from __future__ import annotations

import os

from doxa import config as config_mod
from doxa import engine as engine_mod
from doxa import remote_policy as rp
from doxa.ui.labels import remote_driver_chip


# =======================================================================
# (a) remote listening is ABSENT unless explicitly enabled
# =======================================================================


def test_remote_listening_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DOXA_REMOTE_ENABLED", raising=False)
    assert rp.remote_enabled() is False
    decision = rp.remote_listening_decision(enabled=rp.remote_enabled())
    assert decision.allowed is False
    assert decision.reason  # visible, not silent


def test_remote_listening_turns_on_only_when_explicitly_told(monkeypatch):
    """The companion proof: the SAME function, with the ONE input that
    matters flipped, allows -- so the refusal above is a real branch, not
    a function that always says no."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    assert rp.remote_enabled() is True
    decision = rp.remote_listening_decision(enabled=rp.remote_enabled())
    assert decision.allowed is True


def test_remote_enabled_row_is_off_by_default_and_is_a_real_setting():
    """Same shape as test_permission_mode.py's own
    test_arming_is_off_by_default_and_is_a_real_setting -- the row exists,
    is wired to the right env var, and defaults OFF."""
    row = next(s for s in config_mod.SETTINGS if s.key == "remote_enabled")
    assert row.env == "DOXA_REMOTE_ENABLED"
    assert row.default == ""
    assert row.category == "Remote"


# =======================================================================
# (b) identity: an empty allow-list refuses everyone
# =======================================================================


def test_an_empty_allow_list_refuses_a_real_loopback_identity():
    decision = rp.identity_decision(
        "alice@example.com",
        from_loopback=True,
        allow_list=frozenset(),
    )
    assert decision.allowed is False
    assert decision.reason
    assert "empty" in decision.reason.lower()


def test_a_populated_allow_list_permits_the_matching_login():
    """Companion proof: the SAME function, with a non-empty allow-list
    containing the login, allows. The empty-list refusal above is
    therefore about the LIST being empty, not about identity_decision
    refusing unconditionally."""
    decision = rp.identity_decision(
        "alice@example.com",
        from_loopback=True,
        allow_list=frozenset({"alice@example.com"}),
    )
    assert decision.allowed is True


def test_an_empty_configured_allow_list_refuses_rather_than_allowing_all():
    """The config-reading half: DOXA_REMOTE_ALLOWED_LOGINS unset must not
    be read as 'no restriction' -- it is read as 'nobody'."""
    os.environ.pop("DOXA_REMOTE_ALLOWED_LOGINS", None)
    assert rp.allowed_logins() == frozenset()


# =======================================================================
# (b) identity: a non-allow-listed login is refused, visibly
# =======================================================================


def test_a_non_allow_listed_login_is_refused_with_a_visible_reason():
    decision = rp.identity_decision(
        "mallory@example.com",
        from_loopback=True,
        allow_list=frozenset({"alice@example.com", "bob@example.com"}),
    )
    assert decision.allowed is False
    assert decision.reason
    assert "mallory@example.com" in decision.reason


def test_the_same_login_is_permitted_once_it_is_on_the_list():
    """Companion proof: adding mallory to the SAME allow-list flips the
    SAME function to allow -- the refusal above is keyed on list
    membership, not hard-coded."""
    decision = rp.identity_decision(
        "mallory@example.com",
        from_loopback=True,
        allow_list=frozenset({"alice@example.com", "mallory@example.com"}),
    )
    assert decision.allowed is True


def test_allow_list_matching_is_case_insensitive_both_directions():
    decision = rp.identity_decision(
        "Mallory@Example.com",
        from_loopback=True,
        allow_list=frozenset({"mallory@example.com"}),
    )
    assert decision.allowed is True


def test_allowed_logins_parses_comma_separated_env_and_casefolds(monkeypatch):
    monkeypatch.setenv(
        "DOXA_REMOTE_ALLOWED_LOGINS",
        " Alice@Example.com, bob@example.com ,, ",
    )
    assert rp.allowed_logins() == frozenset({"alice@example.com", "bob@example.com"})


# =======================================================================
# (b) identity: the header is refused when it did not arrive on the
# loopback listener
# =======================================================================


def test_identity_is_refused_when_it_did_not_arrive_on_loopback():
    """The owner's decision, verbatim: the Tailscale-User-Login header is
    trusted ONLY on the loopback listener tailscale serve forwards to,
    NEVER on a public listener. A real login on a real allow-list must
    still be refused when from_loopback is False."""
    decision = rp.identity_decision(
        "alice@example.com",
        from_loopback=False,
        allow_list=frozenset({"alice@example.com"}),
    )
    assert decision.allowed is False
    assert decision.reason
    assert "loopback" in decision.reason.lower()


def test_the_same_identity_is_permitted_once_it_is_from_loopback():
    """Companion proof: flipping ONLY from_loopback, nothing else, flips
    the SAME identity/allow-list pair from refused to allowed -- proving
    the loopback check is a real gate and not a refusal baked into
    identity_decision regardless of input."""
    decision = rp.identity_decision(
        "alice@example.com",
        from_loopback=True,
        allow_list=frozenset({"alice@example.com"}),
    )
    assert decision.allowed is True


def test_no_identity_presented_is_refused_even_from_loopback():
    decision = rp.identity_decision(
        None,
        from_loopback=True,
        allow_list=frozenset({"alice@example.com"}),
    )
    assert decision.allowed is False
    decision2 = rp.identity_decision(
        "   ",
        from_loopback=True,
        allow_list=frozenset({"alice@example.com"}),
    )
    assert decision2.allowed is False


# =======================================================================
# (c) shell_bang is refused without the opt-in, permitted with it
# =======================================================================


def test_shell_bang_is_refused_without_the_remote_shell_opt_in():
    decision = rp.request_kind_decision(
        rp.REQUEST_SHELL_BANG,
        shell_opt_in=False,
        bypass_opt_in=False,
    )
    assert decision.allowed is False
    assert decision.reason
    assert "shell" in decision.reason.lower()


def test_shell_bang_is_permitted_with_the_remote_shell_opt_in():
    """Companion proof, and the security-relevant half of the pair per
    the testing bar: this is what proves the refusal above is not
    vacuous -- the exact same call, with shell_opt_in flipped, allows."""
    decision = rp.request_kind_decision(
        rp.REQUEST_SHELL_BANG,
        shell_opt_in=True,
        bypass_opt_in=False,
    )
    assert decision.allowed is True


def test_remote_allow_shell_row_is_off_by_default():
    os.environ.pop("DOXA_REMOTE_ALLOW_SHELL", None)
    assert rp.remote_allow_shell() is False
    row = next(s for s in config_mod.SETTINGS if s.key == "remote_allow_shell")
    assert row.env == "DOXA_REMOTE_ALLOW_SHELL"
    assert row.default == ""


# =======================================================================
# (c) set_permission_mode(bypassPermissions) is refused without the
# opt-in, permitted with it -- every OTHER mode is unaffected by it
# =======================================================================


def test_set_permission_mode_bypass_is_refused_without_the_opt_in():
    decision = rp.request_kind_decision(
        rp.REQUEST_SET_PERMISSION_MODE,
        shell_opt_in=False,
        bypass_opt_in=False,
        target_mode="bypassPermissions",
    )
    assert decision.allowed is False
    assert decision.reason
    assert "bypasspermissions" in decision.reason.lower()


def test_set_permission_mode_bypass_is_permitted_with_the_opt_in():
    """Companion proof: same target mode, bypass_opt_in flipped, and
    nothing else -- the refusal above is a real gate."""
    decision = rp.request_kind_decision(
        rp.REQUEST_SET_PERMISSION_MODE,
        shell_opt_in=False,
        bypass_opt_in=True,
        target_mode="bypassPermissions",
    )
    assert decision.allowed is True


def test_set_permission_mode_to_any_other_mode_needs_no_bypass_opt_in():
    """The gate is scoped to bypassPermissions specifically -- the spec's
    own wording is 'refuse raising the permission mode to
    bypassPermissions', not 'refuse every mode change'."""
    for mode in ("default", "acceptEdits", "plan", "auto", "dontAsk"):
        decision = rp.request_kind_decision(
            rp.REQUEST_SET_PERMISSION_MODE,
            shell_opt_in=False,
            bypass_opt_in=False,
            target_mode=mode,
        )
        assert decision.allowed is True, mode


def test_set_permission_mode_with_no_target_is_refused_closed():
    decision = rp.request_kind_decision(
        rp.REQUEST_SET_PERMISSION_MODE,
        shell_opt_in=True,
        bypass_opt_in=True,
    )
    assert decision.allowed is False


def test_remote_allow_bypass_row_is_off_by_default():
    os.environ.pop("DOXA_REMOTE_ALLOW_BYPASS", None)
    assert rp.remote_allow_bypass() is False
    row = next(s for s in config_mod.SETTINGS if s.key == "remote_allow_bypass")
    assert row.env == "DOXA_REMOTE_ALLOW_BYPASS"
    assert row.default == ""


def test_bypass_mode_name_matches_the_engines_own_constant():
    """doxa.remote_policy restates engine.BYPASS_MODE rather than
    importing doxa.engine (see the module docstring for why) -- this is
    the seam that stops the restatement silently drifting."""
    assert rp.BYPASS_MODE_NAME == engine_mod.BYPASS_MODE


# =======================================================================
# the reduced surface: request kinds not named above are permitted on
# their own (still gated by remote_enabled/identity through evaluate())
# =======================================================================


def test_the_five_ungated_kinds_are_permitted_by_request_kind_decision():
    for kind in rp.ALWAYS_PERMITTED_KINDS:
        decision = rp.request_kind_decision(
            kind,
            shell_opt_in=False,
            bypass_opt_in=False,
        )
        assert decision.allowed is True, kind


def test_every_request_kind_is_classified_exactly_once():
    """ALWAYS_PERMITTED_KINDS and GATED_KINDS partition REQUEST_KINDS --
    no kind is unclassified, and none is in both."""
    always = set(rp.ALWAYS_PERMITTED_KINDS)
    gated = set(rp.GATED_KINDS)
    assert always | gated == set(rp.REQUEST_KINDS)
    assert always & gated == set()


def test_an_unrecognized_kind_is_refused():
    decision = rp.request_kind_decision(
        "delete_everything",
        shell_opt_in=True,
        bypass_opt_in=True,
    )
    assert decision.allowed is False


# =======================================================================
# evaluate(): the one function a bridge actually calls
# =======================================================================


def test_evaluate_refuses_everything_while_remote_listening_is_off(monkeypatch):
    monkeypatch.delenv("DOXA_REMOTE_ENABLED", raising=False)
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "alice@example.com")
    decision = rp.evaluate(
        rp.REQUEST_READ_STATUS,
        login="alice@example.com",
        from_loopback=True,
    )
    assert decision.allowed is False
    assert (
        "remote listening" in decision.reason.lower()
        or "off" in decision.reason.lower()
    )


def test_evaluate_permits_a_fully_permissive_configuration(monkeypatch):
    """Companion proof for the whole composed function: with remote
    listening on, the login allow-listed, and the request arriving on
    loopback, an ordinary read is granted -- proving evaluate() is not
    hard-wired to refuse regardless of configuration."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "alice@example.com")
    decision = rp.evaluate(
        rp.REQUEST_READ_STATUS,
        login="alice@example.com",
        from_loopback=True,
    )
    assert decision.allowed is True


def test_evaluate_still_refuses_shell_without_its_own_opt_in(monkeypatch):
    """Even with remote listening on and the identity allow-listed --
    exactly the state the previous test proved grants a read -- shell_bang
    stays refused until remote_allow_shell is ALSO on. This is the
    'remote surface is not larger than the local one' assertion, run
    through the full composed entry point rather than the isolated pure
    function above."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "alice@example.com")
    monkeypatch.delenv("DOXA_REMOTE_ALLOW_SHELL", raising=False)
    decision = rp.evaluate(
        rp.REQUEST_SHELL_BANG,
        login="alice@example.com",
        from_loopback=True,
    )
    assert decision.allowed is False


def test_evaluate_permits_shell_once_its_opt_in_is_also_on(monkeypatch):
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "alice@example.com")
    monkeypatch.setenv("DOXA_REMOTE_ALLOW_SHELL", "1")
    decision = rp.evaluate(
        rp.REQUEST_SHELL_BANG,
        login="alice@example.com",
        from_loopback=True,
    )
    assert decision.allowed is True


def test_evaluate_refuses_an_allow_listed_identity_off_loopback(monkeypatch):
    """The defence-in-depth composition: remote enabled, login
    allow-listed, request kind ungated -- and still refused, because the
    identity did not arrive on the loopback listener."""
    monkeypatch.setenv("DOXA_REMOTE_ENABLED", "1")
    monkeypatch.setenv("DOXA_REMOTE_ALLOWED_LOGINS", "alice@example.com")
    decision = rp.evaluate(
        rp.REQUEST_READ_STATUS,
        login="alice@example.com",
        from_loopback=False,
    )
    assert decision.allowed is False
    assert "loopback" in decision.reason.lower()


# =======================================================================
# the status-bar indicator: renders only when connected
# =======================================================================


def test_the_remote_driver_chip_is_absent_when_nobody_is_connected():
    assert remote_driver_chip(None) is None
    assert remote_driver_chip("") is None
    assert remote_driver_chip("   ") is None


def test_the_remote_driver_chip_names_the_connected_identity():
    """Companion proof for the indicator itself: the SAME function, given
    a real identity, renders it -- so the absence above is the hide-at-
    zero branch, not the function being permanently empty."""
    chip = remote_driver_chip("alice@example.com")
    assert chip is not None
    text, hint = chip
    assert "alice@example.com" in text
    assert "alice@example.com" in hint
