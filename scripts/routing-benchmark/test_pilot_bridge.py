"""Credential-free controls for the reviewed repair delivery path."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pilot_bridge as bridge

b = bridge.b
IMAGE = "sha256:" + "a" * 64
HOST = "unix:///owned/rootless.sock"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(root)
        self.state = self.root / "task"
        b.prepare("intermediate-graph", self.state)
        self.proposal = self.root / "proposal.json"

    def write_proposal(self, files):
        self.proposal.write_text(json.dumps({"schema": bridge.PROPOSAL_SCHEMA, "files": files}))

    def replacements(self):
        return {
            "app.py": (self.state / "agent/app.py").read_text() + "\n# approved app revision\n",
            "rules.py": 'raise RuntimeError("candidate must never execute on the operator host")\n',
        }

    def test_context_contains_only_public_workspace_files(self):
        context = bridge.candidate_context(self.state)
        row = b.task("intermediate-graph")
        expected = set(row["editable_paths"]) | {"README.md", "public-cases.json"}
        self.assertEqual(set(context["files"]), expected)
        self.assertEqual(context["files"]["app.py"], row["starter"]["app.py"])
        self.assertEqual(json.loads(context["files"]["public-cases.json"]), row["public_cases"])
        self.assertFalse(set(context) & {"evaluator_cases", "reference", "plan", "task_id", "state"})

    def test_review_and_delivery_change_only_the_new_workspace_without_running_code(self):
        changes = self.replacements()
        self.write_proposal(changes)
        originals = {name: (self.state / "agent" / name).read_bytes() for name in changes}
        with patch.object(b.subprocess, "run") as run:
            reviewed = bridge.review(self.state, self.proposal)
            applied = bridge.apply(self.state, self.proposal, reviewed["approval_sha256"])
            run.assert_not_called()
        self.assertEqual(reviewed["changed_files"], ["app.py", "rules.py"])
        self.assertIn("+# approved app revision\n", reviewed["diff"]["app.py"])
        revision = Path(applied["state"])
        for name, replacement in changes.items():
            self.assertEqual((revision / "agent" / name).read_text(), replacement)
            self.assertEqual((self.state / "agent" / name).read_bytes(), originals[name])
        plan, inventory = b.snapshot(revision, revision / "snapshot")
        self.assertEqual(b.digest(b.encode(plan)), applied["plan_sha256"])
        self.assertEqual(b.digest(b.encode(inventory)), applied["after_sha256"])
        self.assertEqual(json.loads((revision / "repair-review.json").read_bytes()), {
            key: value for key, value in reviewed.items() if key != "diff"
        })
        with self.assertRaisesRegex(ValueError, "already exists"):
            bridge.apply(self.state, self.proposal, reviewed["approval_sha256"])

    def test_approval_refuses_a_changed_proposal_or_baseline_and_leaves_no_revision(self):
        self.write_proposal(self.replacements())
        reviewed = bridge.review(self.state, self.proposal)
        original_proposal = self.proposal.read_text()
        self.proposal.write_text(original_proposal + "\n")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            bridge.apply(self.state, self.proposal, reviewed["approval_sha256"])
        self.proposal.write_text(original_proposal)
        source = self.state / "agent/app.py"
        source.write_text(source.read_text() + "\n# edited after review\n")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            bridge.apply(self.state, self.proposal, reviewed["approval_sha256"])
        self.assertEqual({item.name for item in self.state.iterdir()}, {"agent", "plan.json"})

    def test_refuses_noneditable_paths_bad_content_and_unscoped_workspaces(self):
        for files in (
            {},
            {"../app.py": "pass"},
            {".git/config": "pass"},
            {"README.md": "pass"},
            {"app.py": ["pass"]},
            {"app.py": "\x00"},
            {"app.py": "x" * (b.MAX_FILE + 1)},
            {"app.py": (self.state / "agent/app.py").read_text()},
        ):
            with self.subTest(files=repr(files)[:50]):
                self.write_proposal(files)
                with self.assertRaises(ValueError):
                    bridge.review(self.state, self.proposal)
        self.proposal.write_text(
            '{"schema":"doxa-pilot-repair-proposal-v1","files":{"app.py":"pass","app.py":"raise"}}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            bridge.review(self.state, self.proposal)

        reference = self.root / "reference"
        b.prepare("intermediate-graph", reference, reference=True)
        with self.assertRaisesRegex(ValueError, "non-reference development"):
            bridge.candidate_context(reference)

    def test_grade_binds_snapshot_to_receipt_before_any_candidate_run(self):
        self.write_proposal(self.replacements())
        reviewed = bridge.review(self.state, self.proposal)
        revision = Path(bridge.apply(self.state, self.proposal, reviewed["approval_sha256"])["state"])

        def docker_metadata(argv, **_):
            stdout = b'["name=rootless"]' if "info" in argv else IMAGE.encode()
            return b.subprocess.CompletedProcess(argv, 0, stdout)

        def fixture_case(host, image, snapshot, kind, inputs, driver):
            self.assertEqual((snapshot / "app.py").read_text(), self.replacements()["app.py"])
            self.assertEqual((host, image), (HOST, IMAGE))
            self.assertTrue(driver.is_file())
            return {"observed": True, "output": None, "failure": None, "duration_ms": 0}

        with (
            patch.object(b.subprocess, "run", side_effect=docker_metadata),
            patch.object(b, "invoke", side_effect=fixture_case) as invoke,
        ):
            report = bridge.grade(revision, HOST, IMAGE)
            self.assertEqual(report["approval_sha256"], reviewed["approval_sha256"])
            self.assertEqual(report["source_sha256"], reviewed["after_sha256"])
            self.assertFalse(report["passed"])
            self.assertGreater(invoke.call_count, 0)
            invoke.reset_mock()
            source = revision / "agent/rules.py"
            original = source.read_bytes()
            source.write_text("# changed after approval\n")
            with self.assertRaisesRegex(ValueError, "scored source differs"):
                bridge.grade(revision, HOST, IMAGE)
            source.write_bytes(original)
            plan = revision / "plan.json"
            updated = b.parse(plan.read_bytes())
            updated["family"] = "changed after approval"
            plan.write_bytes(b.encode(updated))
            with self.assertRaisesRegex(ValueError, "scored plan differs"):
                bridge.grade(revision, HOST, IMAGE)
            invoke.assert_not_called()

        receipt = revision / "repair-review.json"
        updated = b.parse(receipt.read_bytes())
        updated["after_sha256"] = "0" * 64
        receipt.write_bytes(b.encode(updated))
        with patch.object(b, "grade") as grade:
            with self.assertRaisesRegex(ValueError, "receipt does not match"):
                bridge.grade(revision, HOST, IMAGE)
            grade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
