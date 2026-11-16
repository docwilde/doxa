"""Artificial count matrices exercise arithmetic, not human or model accuracy."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import agreement


class AgreementTests(unittest.TestCase):
    def record(self, counts=None):
        return {"version": 1, "field": "risky", "classes": ["false", "true"],
                "counts": [[40, 10], [5, 45]] if counts is None else counts}

    def test_binary_hand_calculation_and_direction_are_preserved(self):
        result = agreement.score(self.record())
        # 85/100 observed agreement, 1/2 chance agreement, hence kappa = 7/10.
        self.assertEqual(result["observations"], 100)
        self.assertEqual(result["reviewer_a_marginals"], [50, 50])
        self.assertEqual(result["reviewer_b_marginals"], [45, 55])
        self.assertEqual(result["observed_agreement"], 0.85)
        self.assertEqual(result["chance_agreement"], 0.5)
        self.assertEqual(result["kappa_exact"], "7/10")
        self.assertEqual(result["kappa"], 0.7)
        self.assertIsNone(result["undefined_reason"])

    def test_complete_disagreement_and_four_nominal_categories(self):
        result = agreement.score(self.record([[0, 50], [50, 0]]))
        self.assertEqual(result["kappa_exact"], "-1")
        checkpoint = {"version": 1, "field": "checkpoint", "classes": agreement.FIELDS["checkpoint"],
                      "counts": [[0, 10, 0, 0], [0, 0, 10, 0], [0, 0, 0, 10], [10, 0, 0, 0]]}
        result = agreement.score(checkpoint)
        self.assertEqual(result["observed_agreement"], 0)
        self.assertEqual(result["chance_agreement"], 0.25)
        self.assertEqual(result["kappa_exact"], "-1/3")
        # The scikit-learn reference's nominal-label example gives 11/16.
        # Add one absent category to fit the fixed checkpoint class vocabulary.
        checkpoint["counts"] = [[2, 0, 0, 0], [0, 1, 0, 0], [1, 0, 1, 0], [0, 0, 0, 0]]
        self.assertEqual(agreement.score(checkpoint)["kappa_exact"], "11/16")

    def test_empty_and_constant_identical_labels_have_no_kappa(self):
        for counts, reason in [([[0, 0], [0, 0]], "empty_sample"),
                               ([[100, 0], [0, 0]], "chance_agreement_is_one")]:
            result = agreement.score(self.record(counts))
            self.assertIsNone(result["kappa"])
            self.assertIsNone(result["kappa_exact"])
            self.assertEqual(result["undefined_reason"], reason)

    def test_transposing_reviewers_does_not_change_agreement(self):
        matrix = [[40, 10], [5, 45]]
        a = agreement.score(self.record(matrix))
        b = agreement.score(self.record([list(row) for row in zip(*matrix)]))
        self.assertEqual(a["kappa_exact"], b["kappa_exact"])
        self.assertEqual(a["reviewer_a_marginals"], b["reviewer_b_marginals"])

    def test_malformed_counts_labels_and_extra_fields_are_refused(self):
        invalid = []
        for matrix in [[[True, 0], [0, 1]], [[-1, 0], [0, 1]], [[1.0, 0], [0, 1]],
                       [[1_000_001, 0], [0, 1]], [[500_001, 0], [0, 500_001]], [[1], [2]], []]:
            invalid.append(self.record(matrix))
        for key, value in [("version", True), ("field", "model_verdict"), ("classes", ["true", "false"]),
                           ("raw_message", "must not enter this schema")]:
            record = self.record(); record[key] = value; invalid.append(record)
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(ValueError):
                agreement.score(record)

    def test_multiple_fields_and_duplicate_json_fields_are_checked(self):
        first = self.record(); second = copy.deepcopy(first); second["field"] = "within_assignment"
        raw = (json.dumps(first) + "\n" + json.dumps(second)).encode()
        report = agreement.evaluate(raw)
        self.assertEqual(len(report["fields"]), 2)
        self.assertEqual(report["weighting"], "unweighted")
        self.assertNotIn("passed", report)
        with self.assertRaises(ValueError):
            agreement.evaluate((json.dumps(first) + "\n" + json.dumps(first)).encode())
        with self.assertRaises(ValueError):
            agreement.evaluate(json.dumps(first).replace('"version": 1', '"version": 1, "version": 1').encode())

    def test_cli_enforces_private_files_and_does_not_change_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "counts.jsonl"
            raw = json.dumps(self.record()).encode(); path.write_bytes(raw); path.chmod(0o600)
            def run(target):
                return subprocess.run([sys.executable, str(Path(agreement.__file__)), str(target)],
                                      cwd=root, text=True, capture_output=True, check=False)
            result = run(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["fields"][0]["kappa"], 0.7)
            self.assertEqual(path.read_bytes(), raw)
            path.chmod(0o644); self.assertNotEqual(run(path).returncode, 0); path.chmod(0o600)
            link = root / "link"; os.symlink(path, link)
            self.assertNotEqual(run(link).returncode, 0)
            link.unlink(); os.link(path, link)
            self.assertNotEqual(run(path).returncode, 0)


if __name__ == "__main__":
    unittest.main()
