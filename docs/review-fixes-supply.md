# Beta47 supply and build review fixes

This note records the supply-chain changes applied to the beta47 source tree.
It keeps the existing opt-in Mermaid CLI major and the current source-build
installer. No release key, signature format, or trusted attestation was added.

## F-REMOTE-002: isolate LORE candidate execution

`.github/workflows/lore-bump.yml` now uses two jobs:

1. `verify` has only `contents: read`; it checks out the workflow SHA with
   `persist-credentials: false`, resolves public LORE metadata without a
   GitHub token, and runs Cargo only in that job. A failed test suite can still
   produce the same intentionally red proposal.
2. `publish` runs only for `refs/heads/main`, checks out the same base SHA on a
   fresh runner, and downloads the candidate's three-file artifact. Trusted
   base-branch code validates the JSON schema and size limits, checks hashes,
   verifies that the tag still resolves to the claimed commit and is still the
   newest installable LORE release, requires the manifest to be the exact
   single-pin rewrite, and rejects extra files and symlinks. Trusted Cargo
   updates the lock from the clean base lock and validated manifest, without
   building or running candidate code. The publisher byte-compares its own
   lock with the artifact lock; artifact-controlled dependency edges never
   define the allowed package closure or the committed lock. It confirms the
   diff contains only `rust/doxa-lore/Cargo.toml` and `Cargo.lock`, and checks
   that `main` has not advanced from the candidate base.

The publisher does not check out or execute candidate code. Checkout
credentials are not persisted. `GITHUB_TOKEN` is exposed to only the last
fixed-command publication step; Git receives its in-memory auth header for one
push, with no credential helper or remote URL write. The token permissions are
limited to contents, pull requests, and artifact read.

Both jobs use the same targeted `cargo update --package lore-core` command with
stable Cargo. In the publisher, Cargo only resolves dependency metadata from
the trusted baseline lock and the validated immutable pin; it does not build,
run tests, or execute candidate build scripts. The lock artifact's digest
checks transport integrity, while exact byte equality with the publisher's
independent resolution is the trust gate.

Offline fixtures cover the valid synthetic artifact, stale or malformed
metadata, incorrect digests, added canary files, symlinks, and a malicious
lockfile that widens its own LORE dependency closure to smuggle an unrelated
package change. The publisher rejects it because the candidate lock differs
from Cargo's independent resolution. They validate the publisher; they do not
execute a hostile candidate. The weekly verification still depends on GitHub
Actions isolation between jobs and on maintainers reviewing the resulting PR.

## F-REMOTE-006: make source-build trust explicit

`scripts/install.sh` now reports that it builds source and does not authenticate
a release signature or attestation. It no longer prints a configured
repository URL, which could contain caller-supplied credentials. It still
records the resolved commit in `.doxa-install-sha` for identity and update
tracking.

Set `DOXA_RUST_EXPECTED_SHA` to an independently obtained, lowercase or
uppercase 40-character commit SHA to require an exact match immediately after
checkout and before Cargo starts. A mismatch stops the install. This equality
check lets an operator carry their own trust decision; it does not verify who
published the commit. Existing unsigned source installs remain supported and
are now described as such in `README.md`.

## F-REMOTE-007: pin workflow actions

All external `uses:` references in the three workflow files now use full commit
SHAs with release comments. The tags were resolved through the public GitHub
Git-reference API for their upstream repositories on 2026-10-11:

| Action tag | Commit SHA |
| --- | --- |
| `actions/checkout` v4.2.2 | `11bd71901bbe5b1630ceea73d27597364c9af683` |
| `actions/setup-node` v4.4.0 | `49933ea5288caeca8642d1e84afbd3f7d6820020` |
| `Swatinem/rust-cache` v2.9.2 | `6323deb102c322ba6fcbdcafc7e3dddab59af2b6` |
| `actions/upload-artifact` v4.6.2 | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/download-artifact` v4.3.0 | `d3f86a106a0bac45b974a628896c90dbdf5c8093` |
| `actions/setup-java` v4.7.1 | `c5195efecf7bdfc987ee8bae7a71cb8b11521c00` |
| `android-actions/setup-android` v4.0.4 | `be39fa834029ff78f1a44aa3bb0819b8fc2bd8fd` |

`scripts/check_workflow_action_pins.py` rejects mutable external action refs or
refs missing a readable release comment. Rust CI runs this check.

## F-REMOTE-008: pin container apt inputs

The session image supports explicitly selected Debian 12 Bookworm and Ubuntu
24.04 Noble bases on `amd64` and `arm64`. The helper requires `--base-family`;
the Dockerfile checks that the selected family matches `/etc/os-release`, and
rejects other architectures before replacing the base's moving apt sources.
Both family manifests use the 2026-09-01 archive, updates, and security
snapshots. Their direct package pins are:

| Package | Debian Bookworm | Ubuntu Noble |
| --- | --- | --- |
| `bash` | `5.2.15-2+b13` | `5.2.21-2ubuntu4` |
| `ca-certificates` | `20230311+deb12u1` | `20260601~24.04.1` |
| `git` | `1:2.39.5-0+deb12u3` | `1:2.43.0-1ubuntu7.3` |
| `python3` | `3.11.2-1+b1` | `3.12.3-0ubuntu2.1` |

The selected family, snapshot, supported architectures, manifest hashes,
Dockerfile hash, and final image content ID are recorded in `last-build.json`.
Read-only verification fetched the signed `InRelease` for the suite supplying
each direct pin, checked a valid archive signature with the Debian Bookworm or
Ubuntu archive key, verified the selected `Packages.xz` SHA-256 and size
against the signed index table, and found every exact pin for both
architectures. Verification results are kept under
`/home/docwilde/.dt/doxa-review-fixes-20261011/supply/`.

Updating the direct package set requires changing its matching snapshot and
versions in the same reviewed change. Apt dependencies are resolved only from
that snapshot and the digest-pinned base image. No Docker build or package
installation was run as part of this review.

## Mermaid validation dependency

The optional Mermaid validation lock now overrides only transitive `katex` to
0.18.2. It preserves the pinned Mermaid CLI 12.0.0 and records the npm
registry's integrity for the 0.18.2 tarball. The [KaTeX advisory](https://github.com/advisories/GHSA-238p-pmpm-9mq7)
marks versions before 0.18.2 affected, and KaTeX's [0.18.2 changelog entry](https://github.com/KaTeX/KaTeX/blob/main/CHANGELOG.md#0182)
lists the prototype-pollution fix. The npm registry metadata is at
[`katex@0.18.2`](https://registry.npmjs.org/katex/0.18.2).

The archived npm audit also listed a low-severity path through
`@mermaid-js/layout-tidy-tree`, whose suggested wrapper upgrade crosses the
Mermaid CLI major. That wrapper was not changed here. The current lock was not
reinstalled and no rendered diagram smoke test was run, so this note does not
claim a post-change full npm audit is clear.

## Other captured dependency advisories

The captured Rust audit remains the 2026-10-09 report. This branch does not
edit `Cargo.lock`:

- `rsa` 0.9.10 remains a reported Marvin timing advisory; the parent review
  owns its reachability and API-boundary assessment.
- `lru` 0.12.5 is flagged unsound by the captured RustSec database. The listed
  fixed release is far ahead of the locked 0.12 line, so no blanket override
  was applied without API and dependency compatibility review.
- `paste` 1.0.15 is reported as unmaintained, not as a vulnerability. It is
  left for a targeted replacement review.
- `yoke-derive` 0.8.3 is yanked but retained in the existing lockfile. A yanked
  locked version is tracked for an intentional dependency update; this change
  does not rewrite the workspace graph.

## Verification

Executed with private temporary files under `/home/docwilde/.dt`:

```text
TMPDIR=/home/docwilde/.dt python3 -W error::ResourceWarning -m unittest scripts.tests.test_lore_bump scripts.tests.test_workflow_action_pins scripts.tests.test_mermaid_validation_lock scripts.tests.test_session_image_package -v
python3 scripts/check_workflow_action_pins.py
sh -n scripts/install.sh
git diff --check
```

The Debian and Ubuntu signed-index package checks are recorded in the private
evidence directory listed above. The Rust setup-help assertions were updated
but not run locally because this review lane does not run Cargo. The installer
transaction fixture now also covers an expected-SHA mismatch before Cargo,
but it was not executed here. No Cargo, Rust build, npm install, image build,
Docker command, or installer execution was performed.
