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
    assert "no pyproject.toml" in decision.reason
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
