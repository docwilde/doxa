import copy
import os
from pathlib import Path
import tempfile
import unittest
import blind_review as review


class BlindReviewTests(unittest.TestCase):
    def source(self):
        return [{"version": 1, "id": "24e7e379-b723-4efb-929d-d8a940975c26", "group_id": "4fd129b7-b87a-4e6b-85cf-4ff3d09e9a60",
                 "origin": "real", "split": "development", "consented": True, "scrubbed": True,
                 "charter": {"task": "test-only fixture"}, "assignment": {}, "message": {"body": "fixture"},
                 "model": "deepseek:deepseek-flash", "verdict": {field: 0.1 for field in review.FIELDS},
                 "latency_ms": 100, "source_sha256": "a" * 64}]

    def test_cards_hide_model_verdict_and_labels(self):
        cards = review.cards(review.validate_source(self.source()))
        self.assertFalse(set(cards[0]) & {"model", "verdict", "risky", "latency_ms", "label_source"})

    def test_synthetic_or_unattested_rows_cannot_be_exported_as_real(self):
        for field, value in [("origin", "synthetic"), ("consented", False), ("scrubbed", False)]:
            rows = self.source()
            rows[0][field] = value
            with self.assertRaises(ValueError):
                review.validate_source(rows)

    def test_join_needs_complete_explicit_human_review(self):
        rows = self.source()
        labels = [{"id": rows[0]["id"], "risky": True, "reviewer": "human-reviewer", "rationale": "independent fixture explanation"}]
        with self.assertRaises(ValueError):
            review.join(rows, labels, False)
        with self.assertRaises(ValueError):
            review.join(rows, [], True)
        blank = copy.deepcopy(labels)
        blank[0]["risky"] = None
        with self.assertRaises(ValueError):
            review.join(rows, blank, True)
        joined = review.join(rows, labels, True)
        self.assertEqual(joined[0]["label_source"], "human")
        self.assertFalse(set(joined[0]) & {"message", "charter", "assignment", "source_sha256"})

    def test_fleet_groups_cannot_cross_splits(self):
        rows = self.source()
        second = copy.deepcopy(rows[0])
        second["id"] = "1e676708-d432-4c43-9cde-13bf9bef4e05"
        second["split"] = "holdout"
        with self.assertRaises(ValueError):
            review.validate_source(rows + [second])

    def test_duplicate_json_cannot_silently_replace_provenance(self):
        with self.assertRaises(ValueError):
            review.rows(b'{"origin":"synthetic","origin":"real"}\n')

    def test_private_input_refuses_loose_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "source.jsonl"
            file.write_text('{}\n')
            file.chmod(0o600)
            self.assertEqual(review.read_private(file), b'{}\n')
            file.chmod(0o644)
            with self.assertRaises(ValueError):
                review.read_private(file)
            file.chmod(0o600)
            link = Path(directory) / "link.jsonl"
            os.symlink(file, link)
            with self.assertRaises(OSError):
                review.read_private(link)


if __name__ == "__main__":
    unittest.main()
