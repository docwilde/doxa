"""Offline reporting controls for repair, admission and grader evidence."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pilot_summary as s
from pilot_ledger import PilotLedger

b = s.b
FLASH = "fixed_flash_none"
PRO = "fixed_pro_high"


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = PilotLedger.create(self.root / "private-pilot.sqlite")
        run_id, accounting, _ = self.ledger.reporting_snapshot()
        self.manifest = {**s.header(run_id, accounting), "trials": []}

    def reserve(self, task, arm=FLASH, *, settle=True):
        digest = b.digest(f"private request for {task}/{arm}".encode())
        model, effort, input_rate, output_rate = {
            FLASH: ("deepseek-flash", "none", 300000, 1200000),
            PRO: ("deepseek-v4-pro", "high", 1320000, 3960000),
        }[arm]
        hold = self.ledger.reserve_repair(
            task, arm, 1, model=model, effort=effort, input_bound=321, output_limit=32768,
            input_rate=input_rate, output_rate=output_rate, request_sha256=digest,
        )
        if settle:
            self.ledger.settle_usage(hold.id, request_sha256=digest, model=model,
                                     input_tokens=37, output_tokens=19, latency_ms=4)
        return {"task_id": task, "arm": arm, "allocation": "repair", "call": 1,
                "request_sha256": digest}

    def grade(self, task_id, *, passed=True, failure=None):
        row = b.task(task_id)
        tests = [{"set": split, "index": index, "duration_ms": 2, "passed": True, "failure": None}
                 for split in ("public_cases", "evaluator_cases") for index in range(len(row[split]))]
        tests[-1].update(passed=passed, failure=failure)
        return {
            "schema": "doxa-repair-grade-v1", "task_id": task_id, "split": row["split"],
            "difficulty": row["difficulty"], "family": row["family"], "tests": tests, "passed": passed,
            "source_sha256": "a" * 64, "approval_sha256": "b" * 64,
            "corpus_sha256": self.manifest["corpus_sha256"], "driver_sha256": b.digest(b.repo_read("driver.py")),
            "docker_host": "local_rootless_unix_socket", "image": "sha256:" + "c" * 64, "network": "none",
            "source_readonly": True, "trusted_reference_self_check": False, "evaluator_answers_mounted": False,
            "provider_credentials_passed": False, "host_candidate_execution": False,
            "human_labels": False, "model_quality_validated": False,
            "result_directory": str(self.root / "private-grade-body"),
        }

    def test_aggregates_closed_outcomes_and_ledger_without_disclosing_private_fields(self):
        graded = [self.grade("easy-config"), self.grade("easy-expiry", passed=False),
                  self.grade("difficult-budget", passed=False, failure="timeout")]
        passed = self.reserve("easy-config", settle=False)
        provider = self.reserve("intermediate-graph", settle=False)
        self.manifest["trials"] = [
            {**passed, "status": "graded", "grade": graded[0]},
            {**self.reserve("easy-expiry", PRO), "status": "graded", "grade": graded[1]},
            {**provider, "status": "provider_failure"},
            {"task_id": "easy-config", "arm": s.JEV_ARM, "allocation": "decisions", "call": 1,
             "status": "admission_refusal"},
            {"task_id": "intermediate-delegation", "arm": FLASH,
             "status": "grader_infrastructure_failure", "failure": "image_unavailable"},
            {**self.reserve("difficult-budget"), "status": "graded", "grade": graded[2]},
        ]
        result = s.summarize(self.ledger, self.manifest)
        self.assertEqual(result["planned_trials"], 15)
        self.assertEqual(result["completed_trials"], 2)
        self.assertEqual(result["outcomes"], {
            "functional_pass": 1, "functional_failure": 1, "provider_failure": 1,
            "admission_refusal": 1, "grader_infrastructure_failure": 1, "incomplete": 10,
        })
        cases = sum(len(report["tests"]) for report in graded)
        self.assertEqual(result["grader_observations"], {"cases": cases, "total_duration_ms": 2 * cases})
        self.assertEqual(result["accounting"], self.ledger.summary())
        self.assertEqual(result["accounting"]["allocations"]["repair"]["unresolved"], 2)
        public = json.dumps(result)
        for value in (str(self.root), "private-grade-body", self.manifest["ledger_run_id"],
                      "a" * 64, "b" * 64, "c" * 64, passed["request_sha256"],
                      "easy-config", FLASH, PRO, "image_unavailable"):
            self.assertNotIn(value, public)
        self.assertFalse(result["provider_billing_verified"])
        self.assertFalse(result["model_quality_validated"])

    def test_refuses_wrong_ledger_or_duplicate_and_out_of_scope_rows(self):
        other = PilotLedger.create(self.root / "other.sqlite")
        with self.assertRaisesRegex(ValueError, "does not match this ledger"):
            s.summarize(other, self.manifest)
        row = {"task_id": "easy-config", "arm": FLASH, "status": "incomplete"}
        for rows in ([row, row], [{**row, "task_id": "outside-pilot"}], [{**row, "arm": "outside-pilot"}]):
            self.manifest["trials"] = rows
            with self.assertRaisesRegex(ValueError, "unknown or duplicate pilot trial"):
                s.summarize(self.ledger, self.manifest)

    def test_requires_a_terminal_reserved_request_and_a_complete_frozen_grade(self):
        reserved = self.reserve("easy-config")
        grade = self.grade("easy-config")
        report = {**reserved, "status": "graded", "grade": grade}
        self.manifest["trials"] = [report]
        for key, bad in (
            ("tests", grade["tests"][:-1]), ("tests", [grade["tests"][0]] * len(grade["tests"])),
            ("passed", False), ("task_id", "easy-expiry"), ("corpus_sha256", "d" * 64),
            ("trusted_reference_self_check", True),
        ):
            with self.subTest(key=key):
                self.manifest["trials"] = [{**report, "grade": {**grade, key: bad}}]
                with self.assertRaises(ValueError):
                    s.summarize(self.ledger, self.manifest)
        self.manifest["trials"] = [{**report, "request_sha256": "d" * 64}]
        with self.assertRaisesRegex(ValueError, "does not match a reserved request"):
            s.summarize(self.ledger, self.manifest)
        refusal = {key: value for key, value in reserved.items() if key != "request_sha256"}
        self.manifest["trials"] = [{**refusal, "status": "admission_refusal"}]
        with self.assertRaisesRegex(ValueError, "conflicts with a committed reservation"):
            s.summarize(self.ledger, self.manifest)

    def test_cli_creates_private_template_and_emits_incomplete_roster_totals(self):
        script = b.ROOT / "pilot_summary.py"
        ledger_path = self.root / "private-pilot.sqlite"
        manifest = self.root / "manifest.json"
        base = [sys.executable, str(script)]

        def execute(action):
            return subprocess.run([*base, action, str(ledger_path), str(manifest)],
                                  text=True, capture_output=True, timeout=5)

        self.assertEqual(execute("template").returncode, 0)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(manifest.read_bytes()), self.manifest)
        self.assertNotEqual(execute("template").returncode, 0)
        summary = execute("summarize")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        self.assertEqual(json.loads(summary.stdout)["outcomes"]["incomplete"], 15)


if __name__ == "__main__":
    unittest.main()
