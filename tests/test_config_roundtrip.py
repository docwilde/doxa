# SPDX-License-Identifier: AGPL-3.0-only
"""doxa.config -- the TOML writer's round-trip contract.

test_settings.py already covers the env > file > default precedence rule
and the modal's use of it. This module is narrower: it proves the WRITER
(``_toml_value``/``_write_stored``/``save``) can put back, byte-for-byte
readable, everything the READER (``tomllib`` via ``load()``) can produce
-- scalars, arrays, and tables -- and that a save can no longer amplify a
malformed file into total data loss the way it used to.

Two verified defects, fixed here:

1. ``_write_stored`` used to serialize any value it did not recognize
   (a table, an array) with ``str(value)`` wrapped in quotes. A
   ``[projects]`` table survived on disk right up until the FIRST save of
   an unrelated setting, which rewrote it as an unparseable string --
   silent, permanent loss of a table load() had just handed back fine.
2. ``save()`` seeded its write from ``load()``'s tolerant ``{}`` -- the
   right answer for a READER, since a broken config must cost the user's
   customizations, never their session, but wrong for a WRITER: seeding
   from ``{}`` and then writing means one save on top of a malformed file
   deletes every setting that file held, not just the one being changed.
"""

from __future__ import annotations

import pytest

from doxa import config


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    """Every test here gets its own DOXA_HOME -- nothing may read or
    write the developer's real ~/.doxa (same discipline as
    test_settings.py's fixture of the same name)."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "doxa-home"))
    config.invalidate()
    yield
    config.invalidate()


# -- defect 1: tables and arrays used to be flattened into strings --------


def test_a_projects_table_survives_saving_one_unrelated_setting():
    """The exact reproduction: a hand-written config holding a [projects]
    table -- one entry a plain colour string, one entry a nested table
    carrying an extension field DOXA does not itself define -- must come
    back unchanged after config.save() touches a completely different
    key, and project_colour() must still resolve the entry it understands.
    """
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'engine = "codex"\n'
        "[projects]\n"
        '"/home/docwilde/repo/docwilde/doxa" = '
        '{ colour = "blue", customer = "acme" }\n'
        '"/home/docwilde/repo/docwilde/lore" = "teal"\n',
        encoding="utf-8",
    )
    config.invalidate()

    config.save({"model": "sonnet"})
    config.invalidate()

    stored = config.load()
    assert stored["engine"] == "codex"
    assert stored["model"] == "sonnet"
    projects = stored["projects"]
    assert isinstance(projects, dict), "projects must stay a table, not a string"
    assert projects["/home/docwilde/repo/docwilde/doxa"] == {
        "colour": "blue",
        "customer": "acme",
    }
    assert projects["/home/docwilde/repo/docwilde/lore"] == "teal"
    assert config.project_colour("/home/docwilde/repo/docwilde/lore") == "teal"


def test_an_array_value_survives_a_save():
    """An unknown key holding a list -- an array is a TOML shape DOXA
    reads fine today (tomllib parses it without complaint) but the old
    writer had no representation for, so it fell into the str() fallback
    and came back as a single quoted string."""
    config.save({"model": "sonnet"})
    stored = dict(config.load())
    stored["watched_symbols"] = ["AAPL", "MSFT", 3]
    config._write_stored(stored)
    config.invalidate()

    assert config.load()["watched_symbols"] == ["AAPL", "MSFT", 3]
    assert config.load()["model"] == "sonnet"


def test_a_string_containing_a_newline_round_trips_through_save_and_load():
    """The third, smaller defect: the old escaper handled only backslash
    and double-quote, so a value with a literal newline wrote invalid
    TOML -- the next load() would either mis-parse it or fail outright."""
    config.save({"model": "sonnet"})
    stored = dict(config.load())
    stored["multiline_note"] = "first line\nsecond line"
    config._write_stored(stored)
    config.invalidate()

    assert config.load()["multiline_note"] == "first line\nsecond line"
    # And the file itself is one line for that key -- no literal newline
    # escaped into the middle of the TOML source.
    text = config.config_path().read_text(encoding="utf-8")
    for line in text.splitlines():
        assert "first line" not in line or "second line" in line


def test_a_string_containing_a_quote_and_a_backslash_round_trips():
    config.save({"model": "sonnet"})
    stored = dict(config.load())
    stored["tricky"] = 'she said "hi"\\then left'
    config._write_stored(stored)
    config.invalidate()

    assert config.load()["tricky"] == 'she said "hi"\\then left'


def test_an_unsupported_value_shape_is_refused_rather_than_stringified():
    """A shape outside scalars/arrays-of-scalars/tables (an array of
    tables here) must raise rather than silently fall back to str() --
    that fallback is the root cause of defect 1, so the writer refuses
    instead of repeating it for a shape nobody asked it to support."""
    stored = {"exotic": [{"a": 1}]}
    with pytest.raises(ValueError, match=r"unsupported TOML shape"):
        config._write_stored(stored)


# -- defect 2: save() used to amplify a malformed file into data loss -----


def test_save_on_a_malformed_existing_file_refuses_and_leaves_it_untouched():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not = [ toml\n")
    before = path.read_bytes()
    config.invalidate()

    with pytest.raises(config.ConfigSaveRefused, match=r"does not parse as TOML"):
        config.save({"model": "sonnet"})

    assert path.read_bytes() == before


def test_save_lore_root_on_a_malformed_existing_file_also_refuses():
    """save_lore_root shares save()'s seed-from-the-real-file discipline
    -- it would amplify the same malformed-file-into-data-loss defect
    otherwise, just through a different entry point."""
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not = [ toml\n")
    before = path.read_bytes()
    config.invalidate()

    with pytest.raises(config.ConfigSaveRefused, match=r"does not parse as TOML"):
        config.save_lore_root("/somewhere/else")

    assert path.read_bytes() == before


def test_save_with_no_file_creates_one():
    assert config.config_path().exists() is False
    path = config.save({"model": "sonnet"})
    assert path.exists() is True
    assert config.load()["model"] == "sonnet"
