# SPDX-License-Identifier: AGPL-3.0-only
"""Offline checks for the native LORE upgrade proposer."""
from pathlib import Path
import os
import hashlib
import json
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import lore_bump

ROOT = Path(__file__).resolve().parents[2]


class LoreBumpTests(unittest.TestCase):
    def test_native_pin_is_authoritative_and_rewrite_is_narrow(self):
        original = (ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
        pin = lore_bump.parse_native_pin(original)
        self.assertEqual(pin.slug, 'docwilde/LORE')
        rewritten = lore_bump.rewrite_native_pin(original, 'a' * 40)
        self.assertEqual(lore_bump.parse_native_pin(rewritten).ref, 'a' * 40)
        self.assertEqual(sum(a != b for a, b in zip(original.splitlines(), rewritten.splitlines())), 1)
        for invalid in (
            '[dependencies]\nlore-core = { path = "../lore" }',
            '[dependencies]\nlore-core = { git = "https://github.com/docwilde/LORE", rev = "main" }',
            '[dependencies]\nlore-core = { git = "https://evil.example/LORE", rev = "' + 'a' * 40 + '" }',
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                lore_bump.parse_native_pin(invalid)

    def test_release_tags_and_decisions(self):
        self.assertEqual(lore_bump.newest_tag(['v0.9.0', 'v0.35.0', 'v0.36.0-rc1']), 'v0.35.0')
        self.assertIsNone(lore_bump.newest_tag(['nightly']))
        base = dict(pinned_ref='a' * 40,
                    pinned=lore_bump.RefState(True, '0.35.0'),
                    tags=['v0.36.0'],
                    candidate=lore_bump.RefState(True, '0.36.0'),
                    slug='docwilde/LORE')
        self.assertEqual(lore_bump.decide(**base).action, 'propose')
        self.assertEqual(lore_bump.decide(**{**base, 'candidate': lore_bump.RefState(False)}).action, 'none')
        self.assertEqual(lore_bump.decide(**{**base, 'candidate': lore_bump.RefState(True, '0.99.0')}).action, 'none')
        self.assertEqual(lore_bump.decide(**{**base, 'pinned_ref': 'v0.36.0'}).action, 'none')

    def test_native_packaging_does_not_require_python_metadata(self):
        manifest = '[package]\nname = "lore-core"\nversion = "0.62.0"\n'
        with patch.object(lore_bump, 'fetch_text', side_effect=lambda _slug, path, _ref:
                          manifest if path.endswith('Cargo.toml') else 'fn main() {}' if path.endswith('lore-rs.rs') else None):
            self.assertEqual(lore_bump.fetch_native_state('docwilde/LORE', 'v0.62.0'),
                             lore_bump.RefState(True, '0.62.0'))

    def test_upgrade_rewrites_only_cargo_manifest_and_does_not_repeat(self):
        original = (ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
        old = lore_bump.parse_native_pin(original).ref
        commit = 'b' * 40
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'Cargo.toml'
            output = Path(directory) / 'outputs'
            manifest.write_text(original)
            with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}, clear=False), \
                 patch.object(lore_bump, 'fetch_tags', return_value=['v9.9.9']), \
                 patch.object(lore_bump, 'fetch_native_state', side_effect=lambda _slug, ref:
                              lore_bump.RefState(True, '1.0.0' if ref == old else '9.9.9')), \
                 patch.object(lore_bump, 'fetch_commit', return_value=commit):
                with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}):
                    args = ['--native-manifest', str(manifest), '--write']
                    self.assertEqual(lore_bump.main(args), 0)
                    self.assertEqual(lore_bump.parse_native_pin(manifest.read_text()).ref, commit)
                    self.assertIn(f'commit={commit}', output.read_text())
                    output.unlink()
                    self.assertEqual(lore_bump.main(args), 0)
                    self.assertIn('action=none', output.read_text())

    def test_changed_release_metadata_refuses_rewrite(self):
        original = (ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'Cargo.toml'
            manifest.write_text(original)
            with patch.object(lore_bump, 'fetch_tags', return_value=['v2.0.0']), \
                 patch.object(lore_bump, 'fetch_native_state', side_effect=lambda _slug, ref:
                              lore_bump.RefState(True, '2.0.0' if ref == 'v2.0.0' else '1.0.0')), \
                 patch.object(lore_bump, 'fetch_commit', return_value='b' * 40), \
                 self.assertRaisesRegex(SystemExit, 'metadata changed'):
                lore_bump.main(['--native-manifest', str(manifest), '--write'])
            self.assertEqual(manifest.read_text(), original)

    def test_workflow_tracks_native_manifest_and_lock(self):
        workflow = (ROOT / '.github/workflows/lore-bump.yml').read_text()
        update_command = 'cargo +stable update --manifest-path rust/doxa-lore/Cargo.toml --package lore-core'
        self.assertEqual(workflow.count(update_command), 2)
        self.assertIn('cargo +stable check --locked --package doxa-lore', workflow)
        self.assertIn('--compare-candidate-lock candidate/Cargo.lock Cargo.lock', workflow)
        self.assertIn('git add rust/doxa-lore/Cargo.toml Cargo.lock', workflow)
        self.assertIn('test --locked --workspace --all-features', workflow)
        self.assertNotIn('cp candidate/Cargo.lock Cargo.lock', workflow)
        self.assertIn('--body-file "$body_file"', workflow)
        self.assertNotIn('--body "$body"', workflow)
        self.assertNotIn('uv.lock', workflow)
        top, _, jobs = workflow.partition('\njobs:')
        self.assertIn('contents: read', top)
        self.assertNotIn('contents: write', top)
        self.assertIn('contents: write', jobs)
        self.assertIn('pull-requests: write', jobs)

    def test_candidate_job_is_read_only_and_publisher_is_fresh(self):
        workflow = (ROOT / '.github/workflows/lore-bump.yml').read_text()
        verify = workflow.split('  verify:\n', 1)[1].split('\n  publish:\n', 1)[0]
        publish = workflow.split('\n  publish:\n', 1)[1]
        self.assertIn('contents: read', verify)
        self.assertIn('persist-credentials: false', verify)
        self.assertNotIn('GH_TOKEN', verify)
        self.assertNotIn('GITHUB_TOKEN', verify)
        self.assertNotIn('contents: write', verify)
        self.assertIn('needs: verify', publish)
        self.assertIn('persist-credentials: false', publish)
        self.assertIn('--validate-candidate-artifact', publish)
        self.assertLess(publish.index('Validate candidate identity'), publish.index('GH_TOKEN:'))
        self.assertLess(publish.index('--compare-candidate-lock'), publish.index('GH_TOKEN:'))
        self.assertLess(publish.index('cargo +stable update'), publish.index('GH_TOKEN:'))
        self.assertIn('actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683', publish)

    def _artifact(self, directory, *, extra=()):
        original_manifest = (ROOT / 'rust/doxa-lore/Cargo.toml').read_text()
        old = lore_bump.parse_native_pin(original_manifest).ref
        commit = 'b' * 40
        new_manifest = lore_bump.rewrite_native_pin(original_manifest, commit).encode()
        old_lock = (ROOT / 'Cargo.lock').read_bytes()
        source_old = f'git+https://github.com/docwilde/LORE?rev={old}#{old}'.encode()
        source_new = f'git+https://github.com/docwilde/LORE?rev={commit}#{commit}'.encode()
        new_lock = old_lock.replace(source_old, source_new)
        self.assertNotEqual(old_lock, new_lock)
        (directory / 'native-Cargo.toml').write_bytes(new_manifest)
        (directory / 'Cargo.lock').write_bytes(new_lock)
        metadata = {
            'schema': 1,
            'base_sha': 'a' * 40,
            'tag': 'v9.9.9',
            'commit': commit,
            'native_manifest_sha256': hashlib.sha256(new_manifest).hexdigest(),
            'cargo_lock_sha256': hashlib.sha256(new_lock).hexdigest(),
        }
        (directory / 'candidate.json').write_text(json.dumps(metadata))
        for name, contents in extra:
            (directory / name).write_bytes(contents)
        return original_manifest, old_lock, commit, new_lock

    def test_publisher_accepts_exact_pin_and_trusted_resolution(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            artifact = Path(directory)
            original, old_lock, commit, generated_lock = self._artifact(artifact)
            old_ref = lore_bump.parse_native_pin(original).ref
            with patch.object(lore_bump, 'fetch_commit', return_value=commit), \
                 patch.object(lore_bump, 'fetch_native_state', side_effect=lambda _slug, ref:
                              lore_bump.RefState(True, '0.62.20' if ref == old_ref else '9.9.9')), \
                 patch.object(lore_bump, 'fetch_tags', return_value=['v9.9.9']):
                checked = lore_bump.validate_candidate_artifact(
                    artifact, expected_base='a' * 40,
                    native_manifest_path=ROOT / 'rust/doxa-lore/Cargo.toml')
            self.assertEqual(checked.commit, commit)
            self.assertEqual(checked.tag, 'v9.9.9')
            self.assertEqual(lore_bump.parse_native_pin(checked.native_manifest.decode()).ref, commit)
            lore_bump.require_candidate_lock_matches(checked.cargo_lock, generated_lock)
            self.assertIn(old_ref.encode(), old_lock)

    def test_publisher_rejects_artifact_that_widens_its_own_lore_closure(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            artifact = Path(directory)
            original, _, commit, generated_lock = self._artifact(artifact)
            forged = (artifact / 'Cargo.lock').read_bytes().decode()
            start = forged.index('[[package]]\nname = "lore-core"')
            end = forged.index('[[package]]', start + len('[[package]]'))
            lore_section = forged[start:end]
            self.assertIn('"uuid",\n]', lore_section)
            forged = (forged[:start]
                      + lore_section.replace('"uuid",\n]', '"uuid",\n "doxa-router",\n]')
                      + forged[end:])
            router_version = tomllib.loads((ROOT / 'rust/doxa-router/Cargo.toml').read_text())['package']['version']
            router_entry = f'name = "doxa-router"\nversion = "{router_version}"'
            self.assertEqual(forged.count(router_entry), 1)
            forged = forged.replace(router_entry, 'name = "doxa-router"\nversion = "99.0.0"')
            (artifact / 'Cargo.lock').write_text(forged)
            metadata = json.loads((artifact / 'candidate.json').read_text())
            metadata['cargo_lock_sha256'] = hashlib.sha256(forged.encode()).hexdigest()
            (artifact / 'candidate.json').write_text(json.dumps(metadata))

            old_ref = lore_bump.parse_native_pin(original).ref
            with patch.object(lore_bump, 'fetch_commit', return_value=commit), \
                 patch.object(lore_bump, 'fetch_native_state', side_effect=lambda _slug, ref:
                              lore_bump.RefState(True, '0.62.20' if ref == old_ref else '9.9.9')), \
                 patch.object(lore_bump, 'fetch_tags', return_value=['v9.9.9']):
                candidate = lore_bump.validate_candidate_artifact(
                    artifact, expected_base='a' * 40,
                    native_manifest_path=ROOT / 'rust/doxa-lore/Cargo.toml')
            # This self-issued, correct artifact digest is not a trust signal.
            # The old closure-based validator would have let the forged edge
            # authorize the otherwise unrelated workspace package change.
            with self.assertRaisesRegex(SystemExit, 'differs from the trusted publisher resolution'):
                lore_bump.require_candidate_lock_matches(candidate.cargo_lock, generated_lock)

    def test_publisher_rejects_canary_files_symlinks_and_bad_digests(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            artifact = Path(directory)
            self._artifact(artifact, extra=(('post-checkout', b'run this'),))
            with self.assertRaisesRegex(SystemExit, 'unexpected file set'):
                lore_bump.validate_candidate_artifact(
                    artifact, expected_base='a' * 40,
                    native_manifest_path=ROOT / 'rust/doxa-lore/Cargo.toml')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            artifact = Path(directory)
            self._artifact(artifact)
            (artifact / 'Cargo.lock').unlink()
            (artifact / 'Cargo.lock').symlink_to(ROOT / 'Cargo.lock')
            with self.assertRaisesRegex(SystemExit, 'unsafe file Cargo.lock'):
                lore_bump.validate_candidate_artifact(
                    artifact, expected_base='a' * 40,
                    native_manifest_path=ROOT / 'rust/doxa-lore/Cargo.toml')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            artifact = Path(directory)
            self._artifact(artifact)
            metadata = json.loads((artifact / 'candidate.json').read_text())
            metadata['cargo_lock_sha256'] = '0' * 64
            (artifact / 'candidate.json').write_text(json.dumps(metadata))
            with self.assertRaisesRegex(SystemExit, 'digest mismatch'):
                lore_bump.validate_candidate_artifact(
                    artifact, expected_base='a' * 40,
                    native_manifest_path=ROOT / 'rust/doxa-lore/Cargo.toml')


if __name__ == '__main__':
    unittest.main()
