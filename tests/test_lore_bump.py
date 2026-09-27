# SPDX-License-Identifier: AGPL-3.0-only
"""scripts/lore_bump.py -- the decision half of the LORE upgrade proposer.

Everything here is offline. `decide()` takes measured facts and returns one
of two answers, so the facts are supplied directly and no test reaches
GitHub; the network half (`fetch_tags`, `fetch_ref_state`) is a thin `gh api`
wrapper whose real behaviour is verified by running the script, which is what
`python3 scripts/lore_bump.py` does in a second.

The path with the most tests is the boring one on purpose. Until LORE tags a
release that contains packaging, EVERY scheduled run takes the
`no upgrade available` branch -- so that branch is the one that must not
crash, must not propose, and must not report itself as a failure. A workflow
whose first ten scheduled runs are red is a workflow everyone learns to
ignore, and the way that happens is the no-op path being an afterthought.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lore_bump

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGED = lore_bump.RefState(packaged=True, version=None)


def decide(**kwargs):
    base = dict(
        pinned_ref="1b18ae1056711e1890ca59e68c03fea9e26e0655",
        pinned=lore_bump.RefState(packaged=True, version="0.35.1"),
        tags=["v0.35.0", "v0.34.1"],
        candidate=lore_bump.RefState(packaged=False),
        slug="docwilde/LORE",
    )
    return lore_bump.decide(**{**base, **kwargs})


# --- the pin in this repo's own pyproject.toml ----------------------------


def test_the_real_pyproject_pin_is_parseable():
    # If the pin's shape ever changes, the workflow must fail here rather
    # than on a Monday morning runner.
    pin = lore_bump.parse_pin((REPO_ROOT / "pyproject.toml").read_text())
    assert pin.slug == "docwilde/LORE"
    assert pin.ref


def test_rewriting_the_pin_touches_exactly_one_line():
    original = (REPO_ROOT / "pyproject.toml").read_text()
    rewritten = lore_bump.rewrite_pin(original, "v9.9.9")
    changed = [
        (a, b)
        for a, b in zip(original.splitlines(), rewritten.splitlines())
        if a != b
    ]
    assert len(changed) == 1
    assert "@v9.9.9" in changed[0][1]
    # The comment block above the pin explains why it exists; a rewrite that
    # ate it would leave the next reader with a bare URL.
    assert original.count("#") == rewritten.count("#")


def test_a_missing_pin_is_a_loud_failure_not_a_silent_no_op():
    with pytest.raises(SystemExit):
        lore_bump.parse_pin('dependencies = ["textual>=5,<6"]')


# --- picking the newest tag ------------------------------------------------


def test_newest_tag_orders_by_number_not_by_string():
    # v0.9.0 sorts after v0.35.0 lexicographically; that is the bug.
    assert lore_bump.newest_tag(["v0.9.0", "v0.35.0", "v0.10.0"]) == "v0.35.0"


def test_non_release_tags_are_not_candidates():
    assert lore_bump.newest_tag(["v0.35.0", "v0.36.0-rc1", "nightly"]) == "v0.35.0"


def test_no_tags_at_all_is_answered_not_crashed():
    assert lore_bump.newest_tag([]) is None


# --- the no-op answers -----------------------------------------------------


def test_a_repo_with_no_tags_proposes_nothing():
    decision = decide(tags=[])
    assert decision.action == "none"
    assert "no vX.Y.Z tags" in decision.reason


def test_todays_state_the_newest_tag_carries_no_packaging():
    # LORE v0.35.0 exists and has no pyproject.toml: packaging landed after
    # it and is still unmerged. This is the answer every scheduled run gives
    # until that changes, and it is a success.
    decision = decide()
    assert decision.action == "none"
    assert "no native/oracle packaging" in decision.reason
    assert decision.tag == "v0.35.0"


def test_already_on_the_newest_tag():
    decision = decide(pinned_ref="v0.35.0", candidate=PACKAGED)
    assert decision.action == "none"
    assert "already pinned" in decision.reason


def test_a_pin_ahead_of_every_release_proposes_nothing():
    decision = decide(
        pinned_ref="v0.36.0",
        pinned=lore_bump.RefState(packaged=True, version="0.36.0"),
        candidate=PACKAGED,
    )
    assert decision.action == "none"


def test_a_tag_whose_metadata_names_another_version_is_refused():
    decision = decide(
        candidate=lore_bump.RefState(packaged=True, version="0.99.0"),
    )
    assert decision.action == "none"
    assert "disagree" in decision.reason


# --- the propose answers ---------------------------------------------------


def test_a_newer_packaged_tag_is_proposed():
    decision = decide(
        tags=["v0.35.0", "v0.36.0"],
        candidate=lore_bump.RefState(packaged=True, version="0.36.0"),
    )
    assert decision.action == "propose"
    assert decision.tag == "v0.36.0"


def test_the_tagged_release_of_the_pinned_commit_is_proposed():
    # Same code, better ref: pyproject.toml's own comment asks for the pin to
    # become `@v0.35.1` the moment that release is tagged.
    decision = decide(
        tags=["v0.35.1"],
        candidate=lore_bump.RefState(packaged=True, version="0.35.1"),
    )
    assert decision.action == "propose"
    assert decision.tag == "v0.35.1"


def test_an_unreadable_version_at_the_pin_defers_to_a_human():
    decision = decide(
        pinned=lore_bump.RefState(packaged=True, version=None),
        tags=["v0.36.0"],
        candidate=lore_bump.RefState(packaged=True, version="0.36.0"),
    )
    assert decision.action == "propose"
    assert "human" in decision.reason


# --- the workflow that drives it ------------------------------------------


def test_the_workflow_never_uses_a_floating_action_major():
    # The repo's first CI workflow died in "Set up job" on
    # `astral-sh/setup-uv@v10`: the release is v10.0.1 and there is no
    # floating v10 tag. Every `uses:` here is pinned to a ref that was
    # checked against the git refs API before this landed.
    workflow = (REPO_ROOT / ".github/workflows/lore-bump.yml").read_text()
    uses = [
        line.split("uses:", 1)[1].strip()
        for line in workflow.splitlines()
        if line.strip().startswith("- uses:") or " uses:" in line
    ]
    assert uses, "no actions referenced -- did the workflow move?"
    assert "astral-sh/setup-uv@v10.0.1" in uses
    assert all("@" in ref for ref in uses)


def test_the_workflow_keeps_write_permission_scoped_to_its_one_job():
    workflow = (REPO_ROOT / ".github/workflows/lore-bump.yml").read_text()
    top, _, jobs = workflow.partition("\njobs:")
    assert "contents: read" in top
    assert "contents: write" not in top
    assert "contents: write" in jobs
    assert "pull-requests: write" in jobs


def test_native_pin_is_authoritative_and_rewrite_preserves_manifest():
    original = (REPO_ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
    pin = lore_bump.parse_native_pin(original)
    assert pin.slug == 'docwilde/LORE'
    rewritten = lore_bump.rewrite_native_pin(original, 'a' * 40)
    assert lore_bump.parse_native_pin(rewritten).ref == 'a' * 40
    assert len([(a, b) for a, b in zip(original.splitlines(), rewritten.splitlines()) if a != b]) == 1


@pytest.mark.parametrize('manifest', [
    '[dependencies]\nlore-core = { path = "../lore" }',
    '[dependencies]\nlore-core = { git = "https://github.com/docwilde/LORE", rev = "main" }',
    '[dependencies]\nlore-core = { git = "https://evil.example/LORE", rev = "' + 'a' * 40 + '" }',
])
def test_native_pin_refuses_unpinned_or_unrecognized_sources(manifest):
    with pytest.raises(SystemExit):
        lore_bump.parse_native_pin(manifest)


def test_bump_moves_both_pins_to_same_commit_and_does_not_repeat(monkeypatch, tmp_path):
    oracle = tmp_path / 'pyproject.toml'
    manifest = tmp_path / 'Cargo.toml'
    output = tmp_path / 'outputs'
    original = (REPO_ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
    old = lore_bump.parse_native_pin(original).ref
    commit = 'b' * 40
    oracle.write_text((REPO_ROOT / 'pyproject.toml').read_text())
    manifest.write_text(original)
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    monkeypatch.setattr(lore_bump, 'fetch_tags', lambda slug: ['v9.9.9'])
    monkeypatch.setattr(lore_bump, 'fetch_native_state', lambda slug, ref: lore_bump.RefState(True, '1.0.0' if ref == old else '9.9.9'))
    monkeypatch.setattr(lore_bump, 'fetch_commit', lambda slug, ref: commit)
    args = ['--pyproject', str(oracle), '--native-manifest', str(manifest), '--write']
    assert lore_bump.main(args) == 0
    assert lore_bump.parse_pin(oracle.read_text()).ref == commit
    assert lore_bump.parse_native_pin(manifest.read_text()).ref == commit
    assert f'commit={commit}' in output.read_text()
    output.unlink()
    assert lore_bump.main(args) == 0
    assert 'action=none' in output.read_text()


def test_python_only_release_is_not_native_installable(monkeypatch):
    monkeypatch.setattr(lore_bump, 'fetch_text', lambda slug, path, ref: '[project]\nversion="1.0.0"' if path == 'pyproject.toml' else None)
    assert not lore_bump.fetch_native_state('docwilde/LORE', 'v1.0.0').packaged


def test_upgrade_workflow_tracks_native_pin_and_carrier():
    workflow = (REPO_ROOT / '.github/workflows/lore-bump.yml').read_text()
    assert 'cargo +stable check --package doxa-lore' in workflow
    assert 'cargo +stable update --package lore-core' not in workflow
    assert 'build --locked --package lore-core --bin lore-rs' in workflow
    assert 'test --locked --workspace --all-features' in workflow
    assert 'DOXA_TEST_LORE_RS:' in workflow
    assert 'git add rust/doxa-lore/Cargo.toml Cargo.lock pyproject.toml uv.lock' in workflow
    assert 'resolved_source()' not in workflow


def test_release_commit_rejects_missing_or_malformed_github_response(monkeypatch):
    for response in (None, {'sha': 'main'}, {'sha': 'A' * 40}):
        monkeypatch.setattr(lore_bump, 'gh_api', lambda path, value=response: value)
        with pytest.raises(SystemExit):
            lore_bump.fetch_commit('docwilde/LORE', 'v1.0.0')


def test_changed_release_metadata_never_rewrites_either_pin(monkeypatch, tmp_path):
    oracle = tmp_path / 'pyproject.toml'
    manifest = tmp_path / 'Cargo.toml'
    original = '[dependencies]\nlore-core = { git = "https://github.com/docwilde/LORE", rev = "' + 'a' * 40 + '" }\n'
    oracle_original = '"lore-core @ git+https://github.com/docwilde/LORE@v1.0.0"'
    manifest.write_text(original)
    oracle.write_text(oracle_original)
    monkeypatch.setattr(lore_bump, 'fetch_tags', lambda slug: ['v2.0.0'])
    monkeypatch.setattr(lore_bump, 'fetch_native_state', lambda slug, ref: lore_bump.RefState(True, '2.0.0' if ref == 'v2.0.0' else '1.0.0'))
    monkeypatch.setattr(lore_bump, 'fetch_commit', lambda slug, ref: 'b' * 40)
    with pytest.raises(SystemExit, match='metadata changed'):
        lore_bump.main(['--pyproject', str(oracle), '--native-manifest', str(manifest), '--write'])
    assert manifest.read_text() == original
    assert oracle.read_text() == oracle_original
