# SPDX-License-Identifier: AGPL-3.0-only
"""Offline checks for the native LORE upgrade proposer."""
from pathlib import Path
import os
import tempfile
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
        self.assertIn('cargo +stable check --package doxa-lore', workflow)
        self.assertIn('git add rust/doxa-lore/Cargo.toml Cargo.lock', workflow)
        self.assertIn('test --locked --workspace --all-features', workflow)
        self.assertNotIn('uv.lock', workflow)
        top, _, jobs = workflow.partition('\njobs:')
        self.assertIn('contents: read', top)
        self.assertNotIn('contents: write', top)
        self.assertIn('contents: write', jobs)
        self.assertIn('pull-requests: write', jobs)


if __name__ == '__main__':
    unittest.main()
