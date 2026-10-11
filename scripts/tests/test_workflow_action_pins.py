# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import unittest

from scripts import check_workflow_action_pins as pins

ROOT = Path(__file__).resolve().parents[2]


class WorkflowActionPinTests(unittest.TestCase):
    def test_repository_workflows_use_immutable_external_refs(self):
        self.assertEqual(pins.tree_errors(ROOT), [])

    def test_mutable_tag_and_missing_comment_fail(self):
        self.assertEqual(len(pins.pin_errors("- uses: actions/checkout@v4\n")), 2)
        self.assertEqual(len(pins.pin_errors("- uses: actions/checkout@" + "a" * 40 + "\n")), 1)

    def test_verified_sha_comment_and_local_action_are_accepted(self):
        workflow = "\n".join((
            "- uses: actions/checkout@" + "a" * 40 + " # v4.2.2",
            "- uses: ./.github/actions/local",
        ))
        self.assertEqual(pins.pin_errors(workflow), [])


if __name__ == "__main__":
    unittest.main()
