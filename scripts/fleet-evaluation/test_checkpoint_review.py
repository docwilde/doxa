import copy
from pathlib import Path
import tempfile
import unittest
import uuid

import checkpoint_review as review


GUIDE = b"# Fixture-only checkpoint guide\n"


def sample(number=1, origin="synthetic", *, citations=("host-a",)):
    outcome = {"status": "decision", "choice": "aligned", "basis": "within_charter",
               "alignment_confidence": 0.99, "guard_confidence": [0.99, 0.99, 0.99],
               "supported": bool(citations), "citation_ids": list(citations)}
    snapshot = {"charter": {"task": "fixture\u2028text"}, "assignments": [{"task": "fixture"}],
                "artifacts": [{"id": "host-a", "evidence": {"tests_verified": False}}],
                "guard_observations": [], "recent_handoff_evidence": [], "budget": {},
                "elapsed_deadline": None, "phase": "running"}
    return {"version": 1, "id": str(uuid.UUID(int=number)), "group_id": str(uuid.UUID(int=100)),
            "source_sha256": f"{number:064x}", "origin": origin, "collection_context": "natural_work",
            "split": "development", "consented": origin == "real", "scrubbed": True, "snapshot": snapshot,
            "model": "jev:jev-1.13.0", "threshold": 0.75, "latency_ms": 500, "outcome": outcome}


def verdicts(packet):
    return [{**template, "verdict": "blocked", "reviewer": "fixture-verdict-reviewer",
             "rationale": "fixture verdict rationale"}
            for template in review.rows((packet / "verdict-template.jsonl").read_bytes())]


def citation_labels(packet, supported=True):
    return [{**template, "supports_claim": supported, "reviewer": "fixture-citation-reviewer",
             "rationale": "fixture citation rationale"}
            for template in review.rows((packet / "frozen/citation-template.jsonl").read_bytes())]


class CheckpointReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.packet = self.root / "packet"
        self.result = self.root / "joined"

    def export(self, *source):
        review.export_packet(review.jsonl(source), GUIDE, self.packet)

    def freeze(self, attestation="synthetic_reference"):
        review.freeze_verdicts(self.packet, review.jsonl(verdicts(self.packet)), attestation)

    def read_result(self):
        return review.rows((self.result / "labeled-checkpoints.jsonl").read_bytes())

    def test_two_stage_join_drops_private_bodies_and_preserves_labels_and_raw_choices(self):
        cited = sample(1)
        uncited = sample(2, citations=())
        unavailable = sample(3)
        unavailable["outcome"] = {"status": "unavailable"}
        self.export(cited, uncited, unavailable)
        blind = review.rows((self.packet / "blind-cards.jsonl").read_bytes())
        self.assertEqual(blind[0], {"version": 1, "id": cited["id"], "snapshot": cited["snapshot"]})
        self.assertEqual((self.packet / "label-guide.md").read_bytes(), GUIDE)
        self.assertFalse((self.packet / "frozen").exists())
        with self.assertRaises(OSError):
            review.join_packet(self.packet, None, self.result, False)

        self.freeze()
        with self.assertRaises(FileExistsError):
            self.freeze()
        citation_card, = review.rows((self.packet / "frozen/citation-cards.jsonl").read_bytes())
        self.assertEqual(citation_card, {**blind[0], "choice": "aligned", "basis": "within_charter",
                                         "citation_ids": ["host-a"]})
        labels = review.jsonl(citation_labels(self.packet, supported=False))
        review.join_packet(self.packet, labels, self.result, False)

        results = self.read_result()
        self.assertTrue(all(row["human"] == "blocked" and row["label_source"] == "synthetic_reference"
                            for row in results))
        self.assertEqual(results[0]["outcome"], {"status": "decision", "choice": "aligned",
                         "alignment_confidence": 0.99, "guard_confidence": [0.99, 0.99, 0.99],
                         "supported": True, "citations_valid": True, "citations_supported": False})
        self.assertEqual(results[0]["source_sha256"], cited["source_sha256"])
        self.assertNotIn("snapshot", results[0])
        self.assertTrue(results[1]["outcome"]["citations_valid"])
        self.assertTrue(results[1]["outcome"]["citations_supported"])
        self.assertFalse(results[1]["outcome"]["supported"])
        self.assertEqual(results[2]["outcome"], {"status": "unavailable"})

    def test_real_attestations_and_no_claim_of_human_origin_for_synthetic(self):
        self.export(sample(origin="real"))
        with self.assertRaises(ValueError):
            self.freeze()
        self.freeze("human")
        citations = review.jsonl(citation_labels(self.packet))
        with self.assertRaises(ValueError):
            review.join_packet(self.packet, citations, self.result, False)
        review.join_packet(self.packet, citations, self.result, True)
        self.assertEqual(self.read_result()[0]["label_source"], "human")

    def test_citation_status_requires_exact_labels_and_distinguishes_missing_artifact(self):
        self.export(sample(citations=("missing",)))
        self.freeze()
        labels = citation_labels(self.packet)
        for invalid in (None, review.jsonl(labels), review.jsonl(labels + labels),
                        review.jsonl([{**labels[0], "citation_cards_sha256": "0" * 64}])):
            with self.assertRaises(ValueError):
                review.join_packet(self.packet, invalid, self.result, False)
        self.assertFalse(self.result.exists())
        review.join_packet(self.packet, review.jsonl(citation_labels(self.packet, supported=False)), self.result, False)
        self.assertFalse(self.read_result()[0]["outcome"]["citations_valid"])
        self.assertFalse(self.read_result()[0]["outcome"]["citations_supported"])

    def test_no_citations_means_no_second_stage_labels(self):
        self.export(sample(citations=()))
        self.freeze()
        self.assertEqual((self.packet / "frozen/citation-cards.jsonl").read_bytes(), b"")
        review.join_packet(self.packet, None, self.result, False)
        self.assertTrue(self.read_result()[0]["outcome"]["citations_supported"])

    def test_replay_manifest_binds_only_development_cards_and_export_receipt(self):
        self.export(sample())
        review.export_replay_source(self.packet, self.result)
        manifest, = review.rows((self.result / "development-cards.json").read_bytes())
        self.assertEqual(manifest, {"version": 1, "origin": "synthetic", "split": "development",
                                   "export_receipt_sha256": review.digest((self.packet / "export-receipt.jsonl").read_bytes()),
                                   "cards": review.rows((self.packet / "blind-cards.jsonl").read_bytes())})

    def test_replay_manifest_refuses_real_holdout_cards(self):
        self.export({**sample(origin="real"), "split": "holdout"})
        with self.assertRaises(ValueError):
            review.export_replay_source(self.packet, self.result)
        self.assertFalse(self.result.exists())

    def test_freeze_requires_bound_complete_verdicts_and_unmodified_export(self):
        self.export(sample())
        labels = verdicts(self.packet)
        for invalid in ([], labels + labels, [{**labels[0], "verdict": None}],
                        [{**labels[0], "blind_cards_sha256": "0" * 64}], [{**labels[0], "rationale": " "}]):
            with self.assertRaises(ValueError):
                review.freeze_verdicts(self.packet, review.jsonl(invalid), "synthetic_reference")
        for name in ("sealed-source.jsonl", "label-guide.md", "blind-cards.jsonl"):
            path = self.packet / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.assertRaises(ValueError):
                self.freeze()
            path.write_bytes(original)
        self.assertFalse((self.packet / "frozen").exists())

    def test_join_refuses_changed_frozen_verdicts_cards_or_export_receipt(self):
        self.export(sample())
        self.freeze()
        labels = review.jsonl(citation_labels(self.packet))
        for name in ("frozen/frozen-verdicts.jsonl", "frozen/citation-cards.jsonl", "export-receipt.jsonl"):
            path = self.packet / name
            original = path.read_bytes()
            path.write_bytes(original + b"\n")
            with self.assertRaises(ValueError):
                review.join_packet(self.packet, labels, self.result, False)
            path.write_bytes(original)

    def test_source_bounds_provenance_groups_and_unique_hashes(self):
        base = sample()
        for changed in (
            {**base, "version": True}, {**base, "scrubbed": False}, {**base, "split": "holdout"},
            {**base, "origin": "real"}, {**base, "threshold": True}, {**base, "latency_ms": 120001},
            {**base, "model": "jev:latest"}, {**base, "label_source": "human"},
            {**sample(origin="real"), "split": "holdout", "collection_context": "controlled_task_pilot"},
        ):
            with self.assertRaises(ValueError):
                review.source_rows(review.jsonl([changed]))
        for change in ({"source_sha256": base["source_sha256"]}, {"collection_context": "induced_risk"},
                       {"id": base["id"]}, {"model": "subq:subq-snap-1.2"}):
            with self.assertRaises(ValueError):
                review.source_rows(review.jsonl([base, {**sample(2), **change}]))
        for field, value in (("guard_confidence", [1.0, 1.0, 1.0]), ("basis", "generated reason"),
                             ("citation_ids", ["host-a", "host-b"])):
            invalid_outcome = copy.deepcopy(base)
            invalid_outcome["outcome"][field] = value
            with self.assertRaises(ValueError):
                review.source_rows(review.jsonl([invalid_outcome]))


if __name__ == "__main__":
    unittest.main()
