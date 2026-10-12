# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "scripts/mermaid-validation/package.json"
LOCK = ROOT / "scripts/mermaid-validation/package-lock.json"


class MermaidValidationLockTests(unittest.TestCase):
    def test_mermaid_cli_stays_pinned_and_katex_uses_compatible_security_fix(self):
        package = json.loads(PACKAGE.read_text())
        lock = json.loads(LOCK.read_text())
        self.assertEqual(package["dependencies"]["@mermaid-js/mermaid-cli"], "12.0.0")
        self.assertEqual(lock["packages"][""]["dependencies"]["@mermaid-js/mermaid-cli"], "12.0.0")
        self.assertEqual(package["overrides"]["katex"], "0.18.2")
        katex = lock["packages"]["node_modules/katex"]
        self.assertEqual(katex["version"], "0.18.2")
        self.assertEqual(katex["resolved"], "https://registry.npmjs.org/katex/-/katex-0.18.2.tgz")
        self.assertEqual(katex["integrity"], "sha512-3snve4y0SXTMequLim1FMiPvLGElySam0blN5xQZBqEY3lOJz1TnuaaiugnBZBqHzlSAGmFTYL7MCQkORuLKCA==")
        self.assertIn("commander", katex["dependencies"])
        self.assertNotIn("node_modules/mermaid/node_modules/katex", lock["packages"])


if __name__ == "__main__":
    unittest.main()
