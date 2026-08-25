#!/usr/bin/env python3
"""Decide whether DOXA's pinned `lore-core` should move to a newer LORE tag.

This is the brain of `.github/workflows/lore-bump.yml`; the workflow is the
hands (git, uv, gh pr). Keeping the decision here rather than in a YAML block
scalar buys two things: it runs locally against the real GitHub API without a
runner, and the rules below are ordinary functions with ordinary tests
(tests/test_lore_bump.py) instead of shell that is only ever exercised in
production.

    python3 scripts/lore_bump.py                 # decide and print, touch nothing
    python3 scripts/lore_bump.py --write         # also rewrite the pin on `propose`
    python3 scripts/lore_bump.py --repo docwilde/doxa --pin v0.30.0   # what-if

Why this exists at all: `lore-core` is pinned to an immutable git ref
(pyproject.toml), so a bare install and CI are frozen at that ref until a
human edits the file. Nothing in the project noticed a LORE release; CI's
`LORE_REF: main` leg is a canary that reports breakage, never staleness. This
script is the half that reports staleness.

Network access is `gh api` only -- gh is preinstalled and already
authenticated both on a runner (GH_TOKEN) and on a maintainer's machine, so
there is no token handling here and no third-party action in the workflow.

Stdlib only, on purpose: it runs on the runner's system python3 before uv has
been installed, and the cheap `no upgrade available` path should not cost a
dependency sync.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# `lore-core @ git+https://github.com/docwilde/LORE@<ref>` -- the PEP 508
# direct reference in pyproject.toml. Captured in three pieces so the rewrite
# can replace only <ref> and leave the surrounding comment block, which
# explains WHY the pin exists, exactly as a human wrote it.
PIN_RE = re.compile(
    r'(?P<head>"lore-core\s*@\s*git\+https://github\.com/)'
    r"(?P<owner>[^/\"@]+)/(?P<repo>[^/\"@]+)"
    r"@(?P<ref>[^\"]+)"
    r'(?P<tail>")'
)

# Only plain vX.Y.Z is a candidate. A pre-release or a date tag is not
# something to propose unattended -- LORE tags releases as v0.35.0.
TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class Pin:
    owner: str
    repo: str
    ref: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class RefState:
    """What a git ref of the dependency repo looks like to a packager.

    `packaged` is the load-bearing bit: no pyproject.toml means `uv` cannot
    install that ref as `lore-core` at all, whatever else is true of it.
    `version` can be None even when packaged -- a version this script could
    not read is a reason to be careful, not a reason to call the ref broken.
    """

    packaged: bool
    version: str | None = None


@dataclass(frozen=True)
class Decision:
    """`propose` means: rewrite the pin to `tag`, lock, test, open a PR."""

    action: str  # "none" | "propose"
    reason: str
    tag: str = ""
    tag_version: str = ""
    pinned_version: str = ""


# --------------------------------------------------------------------------
# pure logic -- no network, no filesystem
# --------------------------------------------------------------------------


def parse_pin(pyproject_text: str) -> Pin:
    match = PIN_RE.search(pyproject_text)
    if match is None:
        raise SystemExit(
            "no `lore-core @ git+https://github.com/<owner>/<repo>@<ref>` "
            "dependency found in pyproject.toml -- if the pin moved, this "
            "script's PIN_RE moved with it"
        )
    return Pin(match["owner"], match["repo"], match["ref"])


def rewrite_pin(pyproject_text: str, new_ref: str) -> str:
    return PIN_RE.sub(
        lambda m: f"{m['head']}{m['owner']}/{m['repo']}@{new_ref}{m['tail']}",
        pyproject_text,
        count=1,
    )


def version_of(tag: str) -> tuple[int, int, int] | None:
    match = TAG_RE.match(tag)
    return None if match is None else tuple(int(p) for p in match.groups())  # type: ignore[return-value]


def newest_tag(tags: list[str]) -> str | None:
    """Highest vX.Y.Z, by number rather than by string or by push order.

    GitHub's tag listing is neither sorted by version nor stable enough to
    trust: v0.9.0 sorts after v0.35.0 lexicographically, and "most recently
    created" would happily pick a patch backport to an old line.
    """
    candidates = [(version_of(t), t) for t in tags]
    ranked = [(v, t) for v, t in candidates if v is not None]
    if not ranked:
        return None
    return max(ranked)[1]


def decide(
    *,
    pinned_ref: str,
    pinned: RefState,
    tags: list[str],
    candidate: RefState,
    slug: str = "LORE",
) -> Decision:
    """Turn the measured facts into one of exactly two outcomes.

    `none` is a SUCCESS, not a skip: "no LORE release is newer than the pin"
    is the answer this job exists to compute, and it is the answer it will
    give every week until LORE tags a release that contains packaging. A
    workflow whose ordinary outcome is red is a workflow people mute.
    """
    newest = newest_tag(tags)

    if newest is None:
        return Decision("none", f"{slug} has no vX.Y.Z tags at all; nothing to propose.")

    if newest == pinned_ref:
        return Decision(
            "none",
            f"already pinned to {newest}, the newest {slug} tag.",
            tag=newest,
        )

    if not candidate.packaged:
        # Today's path, and the only one that runs until LORE#46 merges and a
        # release is cut from it. A tag with no pyproject.toml is a LORE
        # packaging gap, not a DOXA incompatibility -- reporting it as a
        # failed upgrade would blame the wrong repo.
        return Decision(
            "none",
            f"the newest {slug} tag {newest} carries no pyproject.toml, so it "
            f"cannot be installed as lore-core at all. Packaging is not in a "
            f"tagged release yet -- nothing to propose until it is.",
            tag=newest,
        )

    new = version_of(newest)
    assert new is not None  # newest_tag only ever returns a TAG_RE match

    if candidate.version is not None and version_of("v" + candidate.version) != new:
        # A tag whose own metadata names a different version is a LORE
        # defect, not something to adopt unattended.
        return Decision(
            "none",
            f"{slug} tag {newest} declares version {candidate.version} in its "
            f"packaging metadata -- tag and metadata disagree, so this is not a "
            f"release to adopt unattended.",
            tag=newest,
            tag_version=candidate.version,
        )

    # The pin may be a tag (compare tag to tag) or an opaque commit (compare
    # against the version that commit's own metadata declares).
    pinned_is_tag = version_of(pinned_ref) is not None
    old = version_of(pinned_ref) if pinned_is_tag else (
        version_of("v" + pinned.version) if pinned.version else None
    )
    pinned_label = pinned_ref if pinned_is_tag else (pinned.version or "")

    if old is None:
        return Decision(
            "propose",
            f"{slug} {newest} is installable; no version could be read at the "
            f"pinned ref {pinned_ref[:12]}, so the two cannot be ordered "
            f"automatically. Proposing the newest tag for a human to judge.",
            tag=newest,
            tag_version=candidate.version or "",
        )

    if new > old:
        return Decision(
            "propose",
            f"{slug} {newest} is newer than the pinned {pinned_label}.",
            tag=newest,
            tag_version=candidate.version or "",
            pinned_version=pinned_label,
        )

    if new == old and not pinned_is_tag:
        # Same code, better ref: the pin is a bare commit whose release has
        # since been tagged. pyproject.toml's own comment asks for this.
        return Decision(
            "propose",
            f"{slug} {newest} is the tagged release of the pinned commit "
            f"({pinned_label}); adopting the tag in place of the SHA.",
            tag=newest,
            tag_version=candidate.version or "",
            pinned_version=pinned_label,
        )

    return Decision(
        "none",
        f"the newest {slug} tag {newest} is not ahead of the pinned "
        f"{pinned_label}; the pin already has everything released.",
        tag=newest,
        tag_version=candidate.version or "",
        pinned_version=pinned_label,
    )


# --------------------------------------------------------------------------
# the network half
# --------------------------------------------------------------------------


def gh_api(path: str, *, paginate: bool = False) -> object | None:
    """One `gh api` call. None on 404, which is a fact here, not a failure."""
    cmd = ["gh", "api"]
    if paginate:
        # Plain --paginate, not --slurp: gh merges top-level arrays across
        # pages into one array on its own, and --slurp does not exist before
        # gh 2.51 (Ubuntu ships 2.46), which would break the local run this
        # script is meant to be verifiable by.
        cmd.append("--paginate")
    cmd.append(path)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if "404" in proc.stderr or "Not Found" in proc.stderr:
            return None
        raise SystemExit(f"gh api {path} failed:\n{proc.stderr.strip()}")
    return json.loads(proc.stdout or "null")


def fetch_tags(slug: str) -> list[str]:
    tags = gh_api(f"repos/{slug}/tags?per_page=100", paginate=True) or []
    return [tag["name"] for tag in tags]  # type: ignore[union-attr]


def fetch_text(slug: str, path: str, ref: str) -> str | None:
    blob = gh_api(f"repos/{slug}/contents/{path}?ref={ref}")
    if blob is None:
        return None
    return base64.b64decode(blob["content"]).decode("utf-8")  # type: ignore[index]


def fetch_ref_state(slug: str, ref: str) -> RefState:
    """Is this ref installable, and what version does it call itself?

    Checked BEFORE any checkout or `uv lock`, deliberately. A tag without
    packaging is a LORE gap, not a DOXA incompatibility, and letting uv
    discover it would spend a runner on a resolve whose failure text is
    locale-dependent (measured: `Konnte Remote-Referenz ... nicht finden`)
    and indistinguishable from the signal this whole workflow exists to
    produce -- "DOXA cannot take this LORE". Two API calls keep the two
    apart, and keep a LORE packaging gap from opening a red PR against DOXA.

    LORE declares `dynamic = ["version"]` and sources it from
    `.claude-plugin/plugin.json` via `[tool.hatch.version]`, so the static
    `[project] version` is genuinely absent there; following the hatch
    `path`/`pattern` indirection is what reads a real number rather than
    guessing one.
    """
    text = fetch_text(slug, "pyproject.toml", ref)
    if text is None:
        return RefState(packaged=False)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return RefState(packaged=True)

    static = data.get("project", {}).get("version")
    if isinstance(static, str):
        return RefState(packaged=True, version=static)

    hatch = data.get("tool", {}).get("hatch", {}).get("version", {})
    source_path = hatch.get("path")
    if not isinstance(source_path, str):
        return RefState(packaged=True)

    source = fetch_text(slug, source_path, ref)
    if source is None:
        return RefState(packaged=True)
    # hatchling's own default when no pattern is given.
    pattern = hatch.get("pattern") or r"""(?i)^__version__\s*=\s*['"](?P<version>[^'"]+)"""
    match = re.search(pattern, source, re.MULTILINE)
    if match is None:
        return RefState(packaged=True)
    return RefState(packaged=True, version=match.group("version"))


# --------------------------------------------------------------------------


def _describe(state: RefState) -> str:
    if not state.packaged:
        return "no pyproject.toml -- not installable as lore-core"
    return f"packaged, version {state.version or '(unreadable)'}"


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, default=repo_root / "pyproject.toml")
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the pin in place when the decision is `propose` "
        "(default: decide and print, change nothing)",
    )
    parser.add_argument("--repo", help="override the dependency repo, for what-if runs")
    parser.add_argument("--pin", help="override the current pin, for what-if runs")
    args = parser.parse_args(argv)

    text = args.pyproject.read_text(encoding="utf-8")
    pin = parse_pin(text)
    if args.repo:
        owner, _, repo = args.repo.partition("/")
        pin = Pin(owner, repo, pin.ref)
    if args.pin:
        pin = Pin(pin.owner, pin.repo, args.pin)

    print(f"pinned:  {pin.slug}@{pin.ref}")

    tags = fetch_tags(pin.slug)
    newest = newest_tag(tags)
    print(f"tags:    {len(tags)} found, newest vX.Y.Z is {newest or '(none)'}")

    pinned = fetch_ref_state(pin.slug, pin.ref)
    candidate = fetch_ref_state(pin.slug, newest) if newest else RefState(packaged=False)
    print(f"at pin:      {_describe(pinned)}")
    print(f"at {newest or '(none)'}: {_describe(candidate)}")

    decision = decide(
        pinned_ref=pin.ref,
        pinned=pinned,
        tags=tags,
        candidate=candidate,
        slug=pin.slug,
    )
    print(f"\ndecision: {decision.action}\nreason:   {decision.reason}")

    if decision.action == "propose" and args.write:
        args.pyproject.write_text(rewrite_pin(text, decision.tag), encoding="utf-8")
        print(f"\nrewrote {args.pyproject} pin -> {decision.tag}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        # key=value is a ONE-line format: a newline in any value would let
        # the rest of that value be read as further outputs. Nothing here
        # produces one today; flattening costs nothing and keeps it true.
        def line(key: str, value: str) -> str:
            return f"{key}={' '.join(value.split())}\n"

        with open(out, "a", encoding="utf-8") as handle:
            handle.write(line("action", decision.action))
            handle.write(line("tag", decision.tag))
            handle.write(line("tag_version", decision.tag_version))
            handle.write(line("pinned_ref", pin.ref))
            handle.write(line("pinned_version", decision.pinned_version))
            handle.write(line("slug", pin.slug))
            handle.write(line("reason", decision.reason))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"### LORE bump check\n\n"
                f"- pinned: `{pin.slug}@{pin.ref}` -- {_describe(pinned)}\n"
                f"- newest tag: `{newest or '(none)'}` -- {_describe(candidate)}\n"
                f"- **{decision.action}** -- {decision.reason}\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
