# SPDX-License-Identifier: AGPL-3.0-only
"""The engine is selectable from inside the terminal (v1.12.0), and the
registry is the one place that says which engines there are.

Four engines have been registered since 1.10.0 and all four worked from
the command line, where ``doxa.engines``' registry is the single validator
-- ``--engine deepsek`` is refused BY NAME with the real list. Inside the
app they were half-invisible: the settings row carried the literal
``("", "claude", "codex")``, the model picker built a ``ClaudeProvider``
for every session whatever engine it was running, and there was no
``/engine`` command at all.

Every test here is named for the failure it catches, and the ones that
matter most are the CLOSURE tests: a fifth engine must reach the settings
row, the model picker and ``/engine`` without a second edit anywhere, or
arrive with a written, testable reason why not
(:data:`doxa.providers.CATALOG_EXEMPT_ENGINES`).

NOTHING HERE TOUCHES A LIVE API. The vendor catalogue is driven through
``VendorModelProvider(fetch=...)`` -- the same injection point
``ChatApiEngine(transport=...)`` established -- and the two tests that DO
want the real thing are skipped cleanly without a credential, the way
tests/test_lore_sync.py skips without an op log.
"""

from __future__ import annotations

import os
import json
import sys
import textwrap
from datetime import datetime, timezone

import pytest

from doxa import commands as commands_mod
from doxa import config as config_mod
from doxa import claude_catalog as claude_catalog_mod
from doxa import engines as engines_mod
from doxa import providers as providers_mod
from doxa import vendors as vendors_mod
from doxa.app import ChipPicker, DoxaApp, SystemBlock
from doxa.engines import EngineCapabilities
from doxa.providers import ModelInfo, VendorModelProvider
from tests.fakes import FakeEngine
from tests.helpers import _chip_offset
from textual.content import Content


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config_mod.invalidate()
    yield
    config_mod.invalidate()


@pytest.fixture(autouse=True)
def _no_vendor_credentials(monkeypatch):
    """The suite must never reach a vendor API, and "the developer happens
    to have a key exported" must never change what a test asserts. Every
    test below that wants the live tier injects its own ``fetch``."""
    for spec in vendors_mod.VENDORS.values():
        monkeypatch.delenv(spec.env_var, raising=False)


def _system_texts(app) -> list[str]:
    return [b.text for b in app.query(SystemBlock) if b.id != "identity-block"]


async def _run(app, pilot, line: str) -> str:
    app.query_one("#prompt-input").value = line
    before = len(_system_texts(app))
    await pilot.press("enter")
    for _ in range(200):
        texts = _system_texts(app)
        if len(texts) > before:
            return texts[-1]
        await pilot.pause(0.02)
    raise AssertionError(f"{line!r} produced no output block")


async def _app(monkeypatch, tmp_path, fake=None):
    monkeypatch.setenv("DOXA_RUNTIME_DIR", str(tmp_path / "rt"))
    fake = fake or FakeEngine([])
    monkeypatch.setattr("doxa.app.SessionEngine", lambda cwd, model=None: fake)
    return DoxaApp(cwd=str(tmp_path)), fake


class _ZephyrProvider:
    """A fifth engine, registered by a test. The whole point of the
    closure tests is that this one line is all it takes for the settings
    row and ``/engine`` to know about it."""

    def engine_id(self) -> str:
        return "zephyr"

    def engine_display_name(self) -> str:
        return "Zephyr (test)"

    def supports(self) -> EngineCapabilities:
        return EngineCapabilities(resume=True)

    def new_session(self, **kwargs):
        raise AssertionError("no test here starts a zephyr session")


@pytest.fixture
def fifth_engine(monkeypatch):
    """Register a fifth engine for the life of one test, and take it out
    again -- ``monkeypatch.setitem`` on the registry dict rather than a
    module reload, so the builtins registered around it are untouched."""
    engines_mod.available()  # force the lazy builtins in before we add
    monkeypatch.setitem(engines_mod._REGISTRY, "zephyr", _ZephyrProvider())
    return "zephyr"


# -- the settings row is the registry ----------------------------------


@pytest.mark.asyncio
async def test_engine_chip_precedes_model_and_selects_new_session_default(
    monkeypatch, tmp_path,
):
    fake = FakeEngine([], model="claude-sonnet-4-5")
    app, _ = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        bar = app.query_one("#status-bar")
        for _ in range(200):
            if app.active_pane.engine is fake and "claude-sonnet-4-5" in str(bar.content):
                break
            await pilot.pause(0.02)
        plain = Content.from_markup(str(bar.content)).plain
        assert plain.index("claude") < plain.index("claude-sonnet-4-5")
        keys = [chip.key for chip in app.active_pane._status_chips()]
        assert keys[keys.index("claude") + 1].startswith("claude-sonnet-4-5")
        assert "open_engine_picker" in str(bar.content)
        await pilot.click("#status-bar", offset=_chip_offset(app, "claude"))
        await pilot.pause()
        picker = app.query_one("#chip-picker", ChipPicker)
        assert picker.is_open and picker.border_title == "engine"
        assert picker._current_id == config_mod.engine()
        assert "NEW sessions" in picker._note
        assert {rid for rid, _ in picker._all_rows} == set(engines_mod.available())
        picker.select_row(next(
            index for index, (rid, _) in enumerate(picker._rows)
            if rid == "codex"
        ))
        for _ in range(200):
            if config_mod.engine() == "codex":
                break
            await pilot.pause(0.02)
        assert config_mod.engine() == "codex"
        assert engines_mod.engine_id_of(app.active_pane.engine) == "claude"
        assert app._new_session_engine_override == "codex"
        assert "claude" in Content.from_markup(str(bar.content)).plain


def test_the_settings_engine_row_offers_exactly_the_registered_engines():
    """The defect: the row's choices were the literal ``("", "claude",
    "codex")``, so neither vendor engine that shipped in 1.10.0 could be
    selected from the settings modal at all."""
    row = config_mod.SETTINGS_BY_KEY["engine"]
    offered = tuple(choice for choice in row.options() if choice)
    assert offered == engines_mod.available()


def test_the_engine_row_still_offers_the_unset_value():
    """The empty first element is every choice row's "not pinned", and
    dropping it while switching to a registry source would have made the
    row impossible to clear."""
    assert config_mod.SETTINGS_BY_KEY["engine"].options()[0] == ""


def test_a_fifth_engine_reaches_the_settings_row_without_a_second_edit(
    fifth_engine,
):
    """THE point of the change. Registering an engine is the only edit;
    if this fails, someone has put a literal back beside the registry."""
    row = config_mod.SETTINGS_BY_KEY["engine"]
    assert fifth_engine in row.options()
    assert fifth_engine in row.placeholder()


def test_a_fifth_engine_can_actually_be_SAVED_not_only_listed(fifth_engine):
    """Listing and validating are two readers, and a row that offered an
    engine the save-time check then refused would be worse than one that
    never offered it."""
    row = config_mod.SETTINGS_BY_KEY["engine"]
    assert config_mod._coerce(row, fifth_engine) == fifth_engine


def test_an_engine_that_is_not_registered_is_refused_at_save_time():
    row = config_mod.SETTINGS_BY_KEY["engine"]
    assert config_mod._coerce(row, "deepsek") is None


def test_every_engine_the_row_offers_actually_starts_a_session():
    """The other direction: an option in the modal that ``engines.get``
    would refuse is a menu that lies."""
    row = config_mod.SETTINGS_BY_KEY["engine"]
    for choice in row.options():
        if choice:
            assert engines_mod.get(choice).engine_id() == choice


def test_the_row_reads_the_registry_lazily_not_at_import():
    """``doxa.config`` is imported by ``doxa --version`` and ``doxa
    doctor``; resolving the choices at import would put ``doxa.vendors``
    (and the ``lore_core`` it drags in) on every launch."""
    row = config_mod.SETTINGS_BY_KEY["engine"]
    assert row.choices == ()
    assert callable(row.choices_source)


# -- every engine has a catalogue, or a written reason -----------------


def test_every_registered_engine_has_a_model_provider_or_is_named_exempt():
    """The closure the model picker depends on. Before this, every engine
    silently got Claude's catalogue -- a DeepSeek session's model chip
    offered haiku/sonnet/opus/fable."""
    for engine_id in engines_mod.available():
        provider = providers_mod.model_provider(engine_id)
        assert provider is not None or engine_id in providers_mod.CATALOG_EXEMPT_ENGINES, (
            f"{engine_id} has no model provider and is not listed in "
            "providers.CATALOG_EXEMPT_ENGINES -- add one or say why not"
        )


def test_the_catalogue_exemption_names_only_engines_that_exist():
    """A stale exemption is how an engine loses its catalogue silently:
    the engine is renamed, the exemption keeps matching nothing, and the
    closure test above passes while the picker shows the wrong list."""
    assert providers_mod.CATALOG_EXEMPT_ENGINES <= set(engines_mod.available())


def test_an_exempt_engine_says_so_instead_of_borrowing_another_list():
    for engine_id in providers_mod.CATALOG_EXEMPT_ENGINES:
        assert providers_mod.model_provider(engine_id) is None
        assert engine_id in providers_mod.no_catalog_text(engine_id)


def test_a_fifth_engine_with_no_catalogue_fails_the_closure_test(fifth_engine):
    """The closure test must be able to FAIL -- a registered engine that
    is neither given a provider nor named exempt is exactly what it is
    there to catch, and this proves it catches it."""
    assert providers_mod.model_provider(fifth_engine) is None
    assert fifth_engine not in providers_mod.CATALOG_EXEMPT_ENGINES


def test_each_engine_gets_its_OWN_catalogue_not_the_default_one():
    assert providers_mod.model_provider("claude").provider_id() == "claude"
    assert providers_mod.model_provider("codex").provider_id() == "openai"
    assert providers_mod.model_provider("deepseek").provider_id() == "deepseek"
    assert providers_mod.model_provider("glm").provider_id() == "zai"


async def test_codex_catalogue_uses_the_cli_list_and_caches_success():
    calls = []

    async def _fetch():
        calls.append("model/list")
        return [ModelInfo("gpt-6-sol", "GPT-6 Sol", "cli")]

    provider = providers_mod.CodexProvider(fetch=_fetch)
    assert provider.default_model() is None
    first = await provider.list_models()
    assert await provider.list_models() == first
    assert calls == ["model/list"]
    assert [m.id for m in first] == ["gpt-6-sol"]
    assert "signed-in Codex CLI" in provider.catalog_note(first)


async def test_codex_catalogue_refreshes_after_the_account_can_change(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(providers_mod.time, "monotonic", lambda: now[0])
    account_models = [[ModelInfo("gpt-6-sol", "Sol", "cli")]]

    async def _fetch():
        return account_models[-1]

    provider = providers_mod.CodexProvider(fetch=_fetch)
    assert [m.id for m in await provider.list_models()] == ["gpt-6-sol"]
    account_models.append([ModelInfo("gpt-6-astra", "Astra", "cli")])
    assert [m.id for m in await provider.list_models()] == ["gpt-6-sol"]
    now[0] += providers_mod.CATALOG_CACHE_TTL + 1
    assert [m.id for m in await provider.list_models()] == ["gpt-6-astra"]


async def test_codex_catalogue_unavailable_is_honest_and_retryable():
    calls = []

    async def _fetch():
        calls.append("model/list")
        return []

    provider = providers_mod.CodexProvider(fetch=_fetch)
    assert await provider.list_models() == []
    assert await provider.list_models() == []
    assert calls == ["model/list", "model/list"]
    assert "unavailable" in provider.catalog_note([])
    assert "sonnet" not in provider.catalog_note([])


async def test_codex_app_server_handshake_and_paginated_model_list(tmp_path):
    script = tmp_path / "codex-catalog-server.py"
    log = tmp_path / "requests.jsonl"
    script.write_text(textwrap.dedent("""
        import json
        import sys

        log = open(sys.argv[1], "w")
        for line in sys.stdin:
            request = json.loads(line)
            log.write(json.dumps(request) + "\\n")
            log.flush()
            if request.get("method") == "initialize":
                print(json.dumps({"id": request["id"], "result": {}}), flush=True)
            elif request.get("method") == "model/list":
                cursor = request["params"].get("cursor")
                result = (
                    {"data": [
                        {"id": "gpt-6-sol", "displayName": "GPT-6 Sol"},
                        {"id": "hidden-model", "hidden": True},
                    ], "nextCursor": "page-2"}
                    if cursor is None else
                    {"data": [
                        {"id": "gpt-6-sol", "displayName": "duplicate"},
                        {"id": "gpt-6-luna"},
                    ], "nextCursor": None}
                )
                print(json.dumps({"id": request["id"], "result": result}), flush=True)
    """))
    models = await providers_mod._list_codex_models(
        (sys.executable, "-u", str(script), str(log)), timeout=2.0,
    )
    assert [(m.id, m.display_name, m.source) for m in models] == [
        ("gpt-6-sol", "GPT-6 Sol", "cli"),
        ("gpt-6-luna", "gpt-6-luna", "cli"),
    ]
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["method"] for r in requests] == [
        "initialize", "initialized", "model/list", "model/list",
    ]
    assert requests[0]["params"]["clientInfo"]["name"] == "doxa"
    assert requests[2]["params"]["includeHidden"] is False
    assert requests[3]["params"]["cursor"] == "page-2"


async def test_codex_app_server_timeout_offers_no_guessed_models(tmp_path):
    script = tmp_path / "slow-codex-server.py"
    script.write_text("import time\ntime.sleep(2)\n")
    models = await providers_mod._list_codex_models(
        (sys.executable, "-u", str(script)), timeout=0.05,
    )
    assert models == []


def test_a_handle_that_declares_nothing_is_read_as_claude():
    """``engine_id_of``'s default, and the reason it is safe: every handle
    that predates it (SessionEngine, EngineClient) is a Claude session."""
    assert engines_mod.engine_id_of(FakeEngine([])) == "claude"
    assert providers_mod.provider_for(FakeEngine([])).provider_id() == "claude"


def test_a_provider_passed_where_a_handle_belongs_is_not_read_as_an_id():
    """``EngineProvider`` names its engine with a METHOD of the same name,
    so a provider handed to ``engine_id_of`` would otherwise stringify a
    bound method into an id nothing can match, and the failure would
    surface three frames away in the picker."""
    assert engines_mod.engine_id_of(engines_mod.get("codex")) == "claude"


def test_every_engines_OWN_handle_reports_that_engines_id(tmp_path):
    """The gap ``engine_id_of`` would otherwise leave open, closed.

    It reads a duck-typed attribute, so a new engine whose handle simply
    forgets to declare one is read as claude and quietly serves Claude's
    catalogue to its model picker -- exactly the defect this whole change
    exists to remove, arriving through the back door. This walks the
    registry, builds each provider's own handle and asks it who it is.
    Nothing is started: ``new_session`` constructs, it does not connect."""
    for engine_id in engines_mod.available():
        engine = engines_mod.get(engine_id).new_session(cwd=str(tmp_path))
        assert engines_mod.engine_id_of(engine) == engine_id, (
            f"a {engine_id} session's handle does not report {engine_id!r}, "
            "so its model picker would list another engine's catalogue"
        )


def test_the_default_engine_is_one_string_not_three():
    """``config.engine()``'s fallback, the settings row's ``default`` and
    ``engines.DEFAULT_ENGINE_ID`` are the same claim written in three
    places; this is what stops them disagreeing."""
    config_mod.invalidate()
    assert config_mod.engine() == engines_mod.DEFAULT_ENGINE_ID
    assert config_mod.SETTINGS_BY_KEY["engine"].default == engines_mod.DEFAULT_ENGINE_ID
    assert engines_mod.DEFAULT_ENGINE_ID in engines_mod.available()


def test_a_vendor_handle_declares_which_vendor_it_is():
    """One ChatApiEngine class serves both vendors, so a handle that read
    its id off the class would send GLM sessions to DeepSeek's catalogue."""
    for engine_id, spec in vendors_mod.VENDORS.items():
        engine = vendors_mod.ChatApiEngine(cwd=os.getcwd(), spec=spec)
        assert engines_mod.engine_id_of(engine) == engine_id
        assert providers_mod.provider_for(engine).provider_id() == spec.provider_id


# -- a provider lists no model the vendor would not answer to ----------


def test_deepseek_never_offers_the_name_it_silently_substitutes():
    """MEASURED 2026-09-17: DeepSeek answers a request for
    ``deepseek-chat`` with ``deepseek-flash`` and only the response says
    so. Offering a name that quietly becomes a different model attributes
    a transcript to a model that never answered."""
    offered = {m.id for m in _sync_list(vendors_mod.DEEPSEEK)}
    assert "deepseek-chat" not in offered
    assert "deepseek-reasoner" not in offered
    assert offered == {"deepseek-flash", "deepseek-v4-pro"}


def test_no_vendor_fallback_offers_a_model_outside_its_measured_catalogue():
    for spec in vendors_mod.VENDORS.values():
        offered = [m.id for m in _sync_list(spec)]
        assert offered == list(spec.models)
        assert all(m.source == "fallback" for m in _sync_list(spec))


def test_a_vendors_default_model_is_one_it_actually_offers():
    """A picker whose current-model marker can never match anything is a
    picker that looks broken."""
    for spec in vendors_mod.VENDORS.values():
        assert spec.default_model in {m.id for m in _sync_list(spec)}
        assert providers_mod.model_provider(spec.engine_id).default_model() in (
            spec.models
        )


async def test_the_live_catalogue_is_preferred_over_the_measured_one(monkeypatch):
    """The vendor is the authority on its own catalogue, and
    ``VendorSpec.models`` is a measurement with a date on it."""
    monkeypatch.setenv(vendors_mod.DEEPSEEK.env_var, "test-key-not-a-real-one")
    provider = VendorModelProvider(
        vendors_mod.DEEPSEEK,
        fetch=lambda spec, key: ("deepseek-flash", "deepseek-v5"),
    )
    models = await provider.list_models()
    assert [m.id for m in models] == ["deepseek-flash", "deepseek-v5"]
    assert all(m.source == "api" for m in models)
    assert provider.catalog_note(models) == ""


async def test_a_vendor_that_does_not_answer_falls_back_and_says_so(monkeypatch):
    """An unreachable vendor, a refused key and an empty catalogue are the
    same fact to a picker with a floor -- but the operator is told which
    list they are looking at."""
    monkeypatch.setenv(vendors_mod.GLM.env_var, "test-key-not-a-real-one")
    provider = VendorModelProvider(vendors_mod.GLM, fetch=lambda spec, key: ())
    models = await provider.list_models()
    assert [m.id for m in models] == list(vendors_mod.GLM.models)
    note = provider.catalog_note(models)
    assert "static fallback" in note
    assert vendors_mod.GLM.env_var in note


async def test_a_vendor_catalogue_without_a_key_never_calls_the_api():
    """A session that cannot authenticate cannot run a turn either; the
    picker must not stall on a round trip that is going to 401."""
    calls: list = []

    def _fetch(spec, key):
        calls.append(spec.engine_id)
        return ("deepseek-flash",)

    provider = VendorModelProvider(vendors_mod.DEEPSEEK, fetch=_fetch)
    models = await provider.list_models()
    assert calls == []
    assert all(m.source == "fallback" for m in models)


async def test_a_vendor_catalogue_is_fetched_once_and_cached(monkeypatch):
    """The picker opens on every click of the model chip."""
    monkeypatch.setenv(vendors_mod.DEEPSEEK.env_var, "test-key-not-a-real-one")
    calls: list = []

    def _fetch(spec, key):
        calls.append(spec.engine_id)
        return ("deepseek-flash",)

    provider = VendorModelProvider(vendors_mod.DEEPSEEK, fetch=_fetch)
    first = await provider.list_models()
    second = await provider.list_models()
    assert first == second
    assert calls == ["deepseek"]


async def test_a_vendor_catalogue_holds_no_credential(monkeypatch):
    """The guarantee ChatApiEngine states, repeated by an object that is
    cached for the life of a pane: the key is read at request time and
    dropped, so nothing here can leak it through a repr or a pickle."""
    secret = "sk-not-a-real-key-0123456789"
    monkeypatch.setenv(vendors_mod.DEEPSEEK.env_var, secret)
    provider = VendorModelProvider(
        vendors_mod.DEEPSEEK, fetch=lambda spec, key: ("deepseek-flash",)
    )
    await provider.list_models()
    assert secret not in repr(vars(provider))
    assert secret not in repr(provider)


def test_a_models_body_never_yields_an_id_nobody_sent():
    """``fetch_models`` parses a vendor's answer; the one thing it must
    never do is invent a name, so every unreadable shape is dropped."""
    assert vendors_mod._model_ids({"data": [{"id": "a"}, {"id": ""}, {}, 7]}) == ("a",)
    assert vendors_mod._model_ids({"data": [{"id": "a"}, {"id": "a"}]}) == ("a",)
    assert vendors_mod._model_ids({"error": "nope"}) == ()
    assert vendors_mod._model_ids("<html>404</html>") == ()
    assert vendors_mod._model_ids(None) == ()


def test_a_models_endpoint_that_fails_is_an_empty_list_not_an_exception():
    """Every failure mode is the fallback's cue, and none of them may
    escape into a picker open."""

    def _boom(request, timeout=None):
        raise OSError("connection reset")

    assert vendors_mod.fetch_models(
        vendors_mod.DEEPSEEK, "test-key", opener=_boom
    ) == ()


def test_the_models_request_carries_the_key_and_the_vendors_own_url():
    seen: dict = {}

    class _Response:
        # ``read(amt=None)``, mirroring http.client.HTTPResponse -- which
        # is what urllib actually hands fetch_models, and which
        # fetch_models calls with a byte cap (vendors.CATALOGUE_BODY_MAX).
        # A double that only accepted read() turned that cap into a
        # TypeError inside the broad except, so the catalogue read as
        # empty and the test proved the fallback rather than the fetch.
        def read(self, amt=None):
            body = b'{"data": [{"id": "glm-5.3-flash"}]}'
            return body if amt is None or amt < 0 else body[:amt]

        def close(self):
            pass

    def _opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        return _Response()

    ids = vendors_mod.fetch_models(vendors_mod.GLM, "test-key", opener=_opener)
    assert ids == ("glm-5.3-flash",)
    assert seen["url"] == vendors_mod.GLM.models_url
    assert seen["method"] == "GET"
    assert seen["auth"] == "Bearer test-key"


# -- /engine -----------------------------------------------------------


def test_engine_is_a_registry_row_with_a_handler():
    """The closure discipline doxa.commands states: the registry
    describes, the pane executes, and neither may have a command the
    other does not."""
    from doxa.session.commands import PANE_COMMANDS

    assert "/engine" in commands_mod.interactive_names()
    assert "/engine" in {entry.name for entry in PANE_COMMANDS}


def test_the_engine_rows_summary_says_it_is_not_the_running_session():
    """Unlike ``/model`` directly above it in the registry, ``/engine``
    cannot touch this session. A user who discovers that by watching the
    command do nothing was told by a summary that lied."""
    row = commands_mod.find("/engine")
    assert "NEW sessions" in row.summary
    assert "never the running session" in row.summary


async def test_engine_with_no_argument_lists_every_registered_engine(
    monkeypatch, tmp_path
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine")
    for engine_id in engines_mod.available():
        assert engine_id in text, f"{engine_id} is registered but not listed"
    assert "claude" in text and "codex" in text
    assert "deepseek" in text and "glm" in text


async def test_engine_listing_shows_the_capability_difference(
    monkeypatch, tmp_path
):
    """The four engines differ sharply -- 18 of 18 fields for claude, 8
    for codex, 11 for each vendor. A list that showed them as
    interchangeable would be lying, and the numbers are read off
    EngineCapabilities rather than written here, so a new field cannot
    make this text wrong without making this assertion fail."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine")
    total = EngineCapabilities.field_count()
    counts = set()
    for engine_id in engines_mod.available():
        caps = engines_mod.get(engine_id).supports()
        assert f"{len(caps.enabled())}/{total}" in text
        counts.add(len(caps.enabled()))
    assert len(counts) > 1, "the listing must not show four identical engines"
    # And the missing half, by field name, so the difference is legible
    # rather than only countable.
    # NAMED FIELDS the listing must show as missing somewhere. ``mcp_tools``
    # used to be this line's example; it stopped being one when codex got
    # doxa.mcpserver and every engine's map turned it True -- a field no
    # engine lacks proves nothing about a listing of what engines lack.
    assert "permission_modes" in text  # codex and the vendors have one posture
    assert "context_window" in text  # neither codex nor the vendors report one


async def test_engine_listing_marks_what_a_new_session_would_use(
    monkeypatch, tmp_path
):
    config_mod.save({"engine": "glm"})
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine")
    assert "new sessions: glm" in text
    assert "▸ glm" in text


async def test_a_fifth_engine_is_listed_by_engine_without_a_second_edit(
    monkeypatch, tmp_path, fifth_engine
):
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine")
    assert fifth_engine in text
    assert "Zephyr (test)" in text


async def test_engine_with_an_unknown_id_is_refused_naming_the_real_ones(
    monkeypatch, tmp_path
):
    """The same refusal ``doxa --engine deepsek`` prints, from the same
    function -- a second check here would either duplicate the list or
    diverge from it."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine deepsek")
    assert "deepsek" in text
    for engine_id in engines_mod.available():
        assert engine_id in text
    config_mod.invalidate()
    assert "engine" not in config_mod.load()


async def test_engine_says_plainly_that_it_affects_new_sessions(
    monkeypatch, tmp_path
):
    """The failure this catches is a user typing ``/engine glm``, seeing a
    cheerful confirmation, and going on believing the session in front of
    them switched."""
    app, fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        listing = await _run(app, pilot, "/engine")
        chosen = await _run(app, pilot, "/engine glm")
    for text in (listing, chosen):
        assert "new sessions" in text.lower()
    assert "connect" in chosen
    assert "this session keeps claude" in chosen
    # And nothing was done to the running session.
    assert fake.model_switches == []
    assert engines_mod.engine_id_of(fake) == "claude"


async def test_engine_says_so_when_the_environment_will_shadow_the_row(
    monkeypatch, tmp_path
):
    """The silent no-op the settings modal already refuses by giving an
    env-won row no input field at all. ``/engine`` has no field to
    withhold, so it says it instead: the value was written and nothing
    will read it."""
    monkeypatch.setenv("DOXA_ENGINE", "claude")
    config_mod.invalidate()
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/engine glm")
    assert "DOXA_ENGINE" in text
    config_mod.invalidate()
    assert config_mod.load()["engine"] == "glm"  # written...
    assert config_mod.engine() == "claude"  # ...and shadowed, as it said


async def test_engine_selection_becomes_the_settings_row(monkeypatch, tmp_path):
    """One source of truth: ``/engine`` and the settings modal's engine
    row are the same state, the way ``/model`` and its row already are."""
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run(app, pilot, "/engine deepseek")
    config_mod.invalidate()
    assert config_mod.load()["engine"] == "deepseek"
    assert config_mod.engine() == "deepseek"


# -- the model picker follows the session's engine ---------------------


async def test_model_lists_the_vendors_own_catalogue_on_a_vendor_session(
    monkeypatch, tmp_path
):
    """The defect: a DeepSeek session's ``/model`` offered haiku, sonnet,
    opus and fable -- four names DeepSeek has never answered to."""
    fake = FakeEngine([], model="deepseek-flash")
    fake.engine_id = "deepseek"
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model")
    assert "deepseek-flash" in text and "deepseek-v4-pro" in text
    for alias in providers_mod.FALLBACK_MODEL_ALIASES:
        assert alias not in text


async def test_model_on_codex_uses_its_own_catalogue_rather_than_borrowing(
    monkeypatch, tmp_path
):
    async def _catalogue():
        return [ModelInfo("gpt-6-sol", "GPT-6 Sol", "cli")]

    monkeypatch.setattr(providers_mod, "_list_codex_models", _catalogue)
    fake = FakeEngine([], model="gpt-5-codex")
    fake.engine_id = "codex"
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model")
    assert "GPT-6 Sol" in text
    assert "signed-in Codex CLI" in text
    for alias in providers_mod.FALLBACK_MODEL_ALIASES:
        assert alias not in text


async def test_model_still_lists_claudes_aliases_on_a_claude_session(
    monkeypatch, tmp_path
):
    """When the CLI has no usable account cache, its aliases remain."""
    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", lambda: None)
    fake = FakeEngine([], model="claude-sonnet-4-5")
    app, fake = await _app(monkeypatch, tmp_path, fake)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model")
    assert "▸ sonnet" in text
    assert "haiku" in text and "opus" in text


async def test_claude_subscription_cache_is_labelled_last_seen(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    catalog = claude_catalog_mod.ClaudeCatalog(
        models=(
            claude_catalog_mod.ClaudeCatalogModel("claude-sonnet-4-5", "Claude Sonnet 4.5"),
            claude_catalog_mod.ClaudeCatalogModel(
                "claude-fable-5", "Claude Fable 5", "Requires usage credits: billed separately"
            ),
        ),
        fetched_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        stale_at=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
        is_stale=True,
    )
    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", lambda: catalog)
    provider = providers_mod.ClaudeProvider()
    models = await provider.list_models()
    assert [(m.id, m.source) for m in models] == [
        ("claude-sonnet-4-5", "cache"), ("claude-fable-5", "cache"),
    ]
    assert "requires usage credits" in models[1].display_name
    note = provider.catalog_note(models)
    assert "stale" in note and "2026-09-23 12:00 UTC" in note
    assert "last seen" in note and "live" not in note


async def test_logged_out_claude_snapshot_populates_picker_with_sign_in_notice(
    monkeypatch, tmp_path,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    catalog = claude_catalog_mod.ClaudeCatalog(
        models=(claude_catalog_mod.ClaudeCatalogModel("claude-sonnet-5", "Sonnet 5"),),
        fetched_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        stale_at=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
        is_stale=True,
        offline=True,
    )
    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", lambda: catalog)
    app, _fake = await _app(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = await _run(app, pilot, "/model")
    assert "Sonnet 5" in text
    assert "signed out" in text and "sign-in required" in text
    assert "model availability unverified" in text
    assert "last seen 2026-09-23 12:00 UTC" in text
    assert "static fallback" not in text


async def test_claude_catalogue_rechecks_the_cli_cache_after_a_minute(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    now = [100.0]
    monkeypatch.setattr(providers_mod.time, "monotonic", lambda: now[0])
    models = ["claude-sonnet-4-5"]

    def cache():
        return claude_catalog_mod.ClaudeCatalog(
            models=(claude_catalog_mod.ClaudeCatalogModel(models[-1], "Claude"),),
            fetched_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
            stale_at=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
            is_stale=False,
        )

    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", cache)
    provider = providers_mod.ClaudeProvider()
    assert [m.id for m in await provider.list_models()] == ["claude-sonnet-4-5"]
    models.append("claude-opus-4-5")
    assert [m.id for m in await provider.list_models()] == ["claude-sonnet-4-5"]
    now[0] += providers_mod.CATALOG_CACHE_TTL + 1
    assert [m.id for m in await provider.list_models()] == ["claude-opus-4-5"]


async def test_startup_cli_warmup_invalidates_existing_claude_picker_cache(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    models = ["claude-sonnet-4-5"]

    def cache():
        return claude_catalog_mod.ClaudeCatalog(
            models=(claude_catalog_mod.ClaudeCatalogModel(models[-1], "Claude"),),
            fetched_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
            stale_at=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
            is_stale=False,
        )

    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", cache)
    provider = providers_mod.ClaudeProvider()
    assert [m.id for m in await provider.list_models()] == ["claude-sonnet-4-5"]
    models.append("claude-opus-4-5")
    providers_mod.ClaudeProvider.startup_catalog_checked("refreshed")
    assert [m.id for m in await provider.list_models()] == ["claude-opus-4-5"]


async def test_unchanged_startup_snapshot_does_not_claim_a_refresh(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    catalog = claude_catalog_mod.ClaudeCatalog(
        models=(claude_catalog_mod.ClaudeCatalogModel("claude-sonnet-5", "Sonnet 5"),),
        fetched_at=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
        stale_at=datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc),
        is_stale=True,
    )
    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", lambda: catalog)
    providers_mod.ClaudeProvider.startup_catalog_checked("unchanged")
    provider = providers_mod.ClaudeProvider()
    note = provider.catalog_note(await provider.list_models())
    assert "stale" in note
    assert "did not update this snapshot" in note

    monkeypatch.setattr(claude_catalog_mod, "read_cached_catalog", lambda: None)
    provider = providers_mod.ClaudeProvider()
    note = provider.catalog_note(await provider.list_models())
    assert "static fallback" in note
    assert "produced no matching cached list" in note


# -- the live tier, skipped cleanly without a credential ---------------

requires_deepseek = pytest.mark.skipif(
    not (os.environ.get(vendors_mod.DEEPSEEK.env_var) or "").strip(),
    reason=f"no ${vendors_mod.DEEPSEEK.env_var} in the environment",
)
requires_glm = pytest.mark.skipif(
    not (os.environ.get(vendors_mod.GLM.env_var) or "").strip(),
    reason=f"no ${vendors_mod.GLM.env_var} in the environment",
)


@requires_deepseek
def test_live_deepseek_catalogue_still_excludes_the_substituted_name(
    monkeypatch,
):
    """The one measurement this feature rests on, re-taken against the
    live API when a key is present. Skipped -- never failed -- without
    one, so CI and every machine lacking a key stay green.

    ``_no_vendor_credentials`` above clears the variable for every test,
    so this one reads the key from the REAL environment itself, at the
    moment of the call, and never stores it."""
    monkeypatch.undo()
    key = os.environ[vendors_mod.DEEPSEEK.env_var]
    ids = vendors_mod.fetch_models(vendors_mod.DEEPSEEK, key)
    assert ids, "the live catalogue came back empty"
    assert "deepseek-chat" not in ids
    assert vendors_mod.DEEPSEEK.default_model in ids


@requires_glm
def test_live_glm_catalogue_contains_the_default_model(monkeypatch):
    monkeypatch.undo()
    key = os.environ[vendors_mod.GLM.env_var]
    ids = vendors_mod.fetch_models(vendors_mod.GLM, key)
    assert ids, "the live catalogue came back empty"
    assert vendors_mod.GLM.default_model in ids


# -- helpers -----------------------------------------------------------


def _sync_list(spec) -> "list[ModelInfo]":
    """One vendor's catalogue with no key and no network -- the static
    tier, resolved synchronously for the plain assertions above."""
    import asyncio

    provider = VendorModelProvider(spec)
    return asyncio.run(provider.list_models())
