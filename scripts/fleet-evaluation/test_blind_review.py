import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import blind_review as review


class BlindReviewTests(unittest.TestCase):
    def source(self):
        return [{"version": 1, "id": "24e7e379-b723-4efb-929d-d8a940975c26", "group_id": "4fd129b7-b87a-4e6b-85cf-4ff3d09e9a60",
                 "origin": "real", "collection_context": "controlled_task_pilot", "split": "development", "consented": True, "scrubbed": True,
                 "charter": {"task": "test-only fixture"}, "assignment": {}, "message": {"body": "fixture"},
                 "model": "deepseek:deepseek-flash", "verdict": {field: 0.1 for field in review.FIELDS},
                 "latency_ms": 100, "source_sha256": "a" * 64}]

    def test_cards_hide_model_verdict_and_labels(self):
        cards = review.cards(review.validate_source(self.source()))
        self.assertFalse(set(cards[0]) & {"model", "verdict", "risky", "latency_ms", "label_source"})
        self.assertEqual(cards[0]["collection_context"], "controlled_task_pilot")

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
        for raw in [b'{"origin":"synthetic","origin":"real"}\n', b'{"risky":true,"risky":false}\n']:
            with self.assertRaises(ValueError):
                review.rows(raw)

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

    def packet(self, directory):
        packet = Path(directory).resolve() / "packet"
        review.export_packet((json.dumps(self.source()[0]) + "\n").encode(), packet)
        return packet

    def test_changed_sealed_verdict_with_same_message_and_source_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = self.packet(directory)
            path = packet / "sealed-source.jsonl"
            changed = json.loads(path.read_text())
            changed["verdict"]["needs_human_review"] = 0.9
            path.write_text(json.dumps(changed) + "\n")
            with self.assertRaises(ValueError):
                review.frozen_export(packet)

    def test_changed_cards_or_wrong_receipt_binding_is_refused(self):
        for which in ["cards", "receipt", "coherent_hash_wrong_card"]:
            with self.subTest(which=which), tempfile.TemporaryDirectory() as directory:
                packet = self.packet(directory)
                if which != "receipt":
                    path = packet / "blind-cards.jsonl"
                    changed = json.loads(path.read_text())
                    changed["message"]["body"] = "changed after export"
                    path.write_text(json.dumps(changed) + "\n")
                if which != "cards":
                    receipt_path = packet / "export-receipt.jsonl"
                    receipt = json.loads(receipt_path.read_text())
                    receipt["blind_cards_sha256"] = "0" * 64 if which == "receipt" else review.hashlib.sha256((packet / "blind-cards.jsonl").read_bytes()).hexdigest()
                    receipt_path.write_text(json.dumps(receipt) + "\n")
                with self.assertRaises(ValueError):
                    review.frozen_export(packet)

    def test_correct_frozen_export_can_join_complete_human_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = self.packet(directory)
            values, receipt = review.frozen_export(packet)
            self.assertEqual(receipt["rows"], 1)
            labels = [{"id": values[0]["id"], "risky": False, "reviewer": "independent human", "rationale": "test-only label fixture"}]
            joined = review.join(values, labels, True)
            self.assertFalse(joined[0]["risky"])


if __name__ == "__main__":
    unittest.main()
