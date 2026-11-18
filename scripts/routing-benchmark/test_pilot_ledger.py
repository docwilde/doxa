"""Credential-free admission, restart, and accounting controls for the pilot."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from pilot_ledger import LedgerRefused, PilotLedger
from pilot_quote import quote

ROOT = Path(__file__).resolve().parent
DIGEST = "a" * 64
PRO = dict(model="deepseek-v4-pro", effort="high", input_bound=20_000, output_limit=32_768,
           input_rate=1_320_000, output_rate=3_960_000, request_sha256=DIGEST)
JEV = dict(model="jev-1.13.0", input_bound=25_600, input_rate=42_000, request_sha256=DIGEST)


class PilotLedgerTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.path = Path(root.name) / "pilot.sqlite"
        self.ledger = PilotLedger.create(self.path)

    def reserve(self, task="easy-config", arm="fixed_pro_high", call=1, **changes):
        return self.ledger.reserve_repair(task, arm, call, **{**PRO, **changes})

    def settle(self, reservation, **changes):
        details = dict(request_sha256=DIGEST, model=PRO["model"], input_tokens=1_000,
                       output_tokens=4_000, latency_ms=375)
        return self.ledger.settle_usage(reservation.id, **{**details, **changes})

    def test_reserves_exact_candidate_rate_and_releases_only_verified_difference(self):
        hold = self.reserve()
        self.assertEqual(hold.held_usd_micros, 156_162)
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 156_162)
        self.assertEqual(self.settle(hold), 17_160)
        with self.assertRaisesRegex(LedgerRefused, "verified observation"):
            self.settle(hold)
        summary = self.ledger.summary()
        repair = summary["allocations"]["repair"]
        self.assertEqual(repair["available_usd_micros"], 15_000_000 - 17_160)
        self.assertEqual(repair["input_tokens"], 1_000)
        self.assertEqual(repair["output_tokens"], 4_000)
        self.assertEqual(repair["timed_calls"], 1)
        self.assertEqual(repair["total_latency_ms"], 375)
        self.assertEqual(repair["unresolved"], 0)
        self.assertEqual(summary["pilot_sha256"], quote()["pilot_sha256"])
        self.assertEqual(summary["corpus_sha256"], quote()["corpus_sha256"])
        serialized = json.dumps(summary)
        self.assertNotIn(DIGEST, serialized)
        self.assertNotIn(hold.id, serialized)

    def test_frozen_trials_rates_token_limits_and_fixed_arms(self):
        for changes in ({"task": "difficult-dispatch"}, {"arm": "other"}, {"call": 0}, {"call": 7},
                        {"call": True}, {"arm": "fixed_flash_none"}, {"effort": "medium"},
                        {"model": "deepseek-chat"}, {"input_bound": 20_001}, {"input_bound": True},
                        {"output_limit": 32_767}, {"output_limit": 32_769},
                        {"input_rate": 1_319_999}, {"output_rate": 3_960_001}, {"request_sha256": "plaintext"}):
            with self.subTest(changes=changes), self.assertRaises(LedgerRefused):
                self.reserve(**changes)
        flash = self.reserve(arm="fixed_flash_none", model="deepseek-flash", effort="none",
                             input_rate=300_000, output_rate=1_200_000)
        self.assertEqual(flash.held_usd_micros, 45_322)
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 45_322)

    def test_jev_and_fleets_have_separate_allocation_and_no_retries(self):
        for changes in ({"model": "other"}, {"input_bound": 25_601}, {"input_bound": 1024}, {"input_rate": 1}):
            with self.subTest(changes=changes), self.assertRaises(LedgerRefused):
                self.ledger.reserve_decision("easy-config", 1, **{**JEV, **changes})
        with self.assertRaisesRegex(LedgerRefused, "remains unsettled"):
            self.reserve(arm="jev_per_call")
        decision = self.ledger.reserve_decision("easy-config", 1, **JEV)
        self.assertEqual(decision.held_usd_micros, 1076)
        with self.assertRaisesRegex(LedgerRefused, "already reserved"):
            self.ledger.reserve_decision("easy-config", 1, **JEV)
        self.assertEqual(self.settle(decision, model=JEV["model"]), 42)
        self.reserve(arm="jev_per_call")
        for slot in (1, 2):
            fleet = self.ledger.reserve_fleet(slot, ceiling_usd_micros=2_500_000, configuration_sha256=DIGEST)
            self.assertEqual(fleet.held_usd_micros, 2_500_000)
            with self.assertRaisesRegex(LedgerRefused, "pilot model reservation"):
                self.settle(fleet)
        for slot in (1, 3):
            with self.assertRaises(LedgerRefused):
                self.ledger.reserve_fleet(slot, ceiling_usd_micros=2_500_000, configuration_sha256=DIGEST)
        allocations = self.ledger.summary()["allocations"]
        self.assertEqual(allocations["fleet_development"]["charged_usd_micros"], 5_000_000)
        self.assertEqual(allocations["fleet_development"]["available_usd_micros"], 0)
        self.assertEqual(allocations["decisions"]["charged_usd_micros"], 42)
        self.assertEqual(allocations["undispatched"]["available_usd_micros"], 0)
        self.assertEqual(allocations["undispatched"]["ceiling_usd_micros"], 3_000_000)

    def test_all_frozen_slots_fit_their_ceilings_at_full_token_usage(self):
        plan = json.loads((ROOT / "pilot.json").read_bytes())
        for task in plan["tasks"]:
            for arm in plan["arms"]:
                for call in range(1, 7):
                    if arm == "jev_per_call":
                        decision = self.ledger.reserve_decision(task, call, **JEV)
                        self.settle(decision, model=JEV["model"], input_tokens=25_600)
                    candidate = PRO
                    if arm == "fixed_flash_none":
                        candidate = {**PRO, "model": "deepseek-flash", "effort": "none",
                                     "input_rate": 300_000, "output_rate": 1_200_000}
                    hold = self.reserve(task, arm, call, **candidate)
                    charged = self.settle(hold, model=candidate["model"], input_tokens=20_000, output_tokens=32_768)
                    self.assertEqual(charged, hold.held_usd_micros)
        allocations = self.ledger.summary()["allocations"]
        self.assertEqual(allocations["repair"]["reservations"], 90)
        self.assertEqual(allocations["decisions"]["reservations"], 30)
        self.assertEqual(allocations["repair"]["charged_usd_micros"], 60 * 156_162 + 30 * 45_322)
        self.assertEqual(allocations["decisions"]["charged_usd_micros"], quote()["maximum_decision_admission_usd_micros"])
        self.assertLess(allocations["repair"]["charged_usd_micros"], allocations["repair"]["ceiling_usd_micros"])
        self.assertLess(allocations["decisions"]["charged_usd_micros"], allocations["decisions"]["ceiling_usd_micros"])

    def test_ambiguous_dispatch_has_no_refund_and_pauses_its_trial_on_restart(self):
        hold = self.reserve()
        self.ledger = PilotLedger.open(self.path)
        for changes in ({"request_sha256": "b" * 64}, {"input_tokens": None}, {"input_tokens": 0},
                        {"output_tokens": True}, {"output_tokens": -1}):
            with self.subTest(changes=changes), self.assertRaises(LedgerRefused):
                self.settle(hold, **changes)
        with self.assertRaisesRegex(LedgerRefused, "already reserved"):
            self.reserve()
        with self.assertRaisesRegex(LedgerRefused, "remains unsettled"):
            self.reserve(call=2)
        self.reserve(task="easy-expiry")
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 312_324)
        self.assertEqual(self.ledger.summary()["allocations"]["repair"]["unresolved"], 2)
        self.settle(hold)
        self.reserve(call=2)
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 329_484)

    def test_above_bound_verified_usage_freezes_every_allocation_durably(self):
        hold = self.reserve()
        with self.assertRaisesRegex(LedgerRefused, "exceeded the frozen scope"):
            self.settle(hold, output_tokens=32_769)
        self.ledger = PilotLedger.open(self.path)
        for reserve in (lambda: self.reserve(task="easy-expiry"),
                        lambda: self.ledger.reserve_decision("easy-config", 1, **JEV),
                        lambda: self.ledger.reserve_fleet(1, ceiling_usd_micros=2_500_000, configuration_sha256=DIGEST)):
            with self.assertRaisesRegex(LedgerRefused, "exceeded the frozen scope"):
                reserve()
        summary = self.ledger.summary()
        self.assertTrue(summary["scope_breach"])
        self.assertEqual(summary["charged_usd_micros"], hold.held_usd_micros)
        self.assertEqual(summary["allocations"]["repair"]["output_tokens"], 32_769)
        self.assertEqual(summary["allocations"]["repair"]["scope_breaches"], 1)
        self.assertTrue(all(row["available_usd_micros"] == 0 for row in summary["allocations"].values()))

    def test_concurrent_admission_claims_each_scope_once(self):
        worker_count = 8
        barrier = Barrier(worker_count)

        def contend(_):
            ledger = PilotLedger.open(self.path)
            barrier.wait(timeout=10)
            try:
                return ledger.reserve_repair("easy-config", "fixed_pro_high", 1, **PRO).id
            except LedgerRefused as error:
                self.assertIn("already reserved", str(error))
                return None

        with ThreadPoolExecutor(max_workers=worker_count) as workers:
            results = list(workers.map(contend, range(worker_count)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 156_162)

    def test_abrupt_exit_after_admission_preserves_hold(self):
        script = """
import json, os, sys
from pilot_ledger import PilotLedger
ledger = PilotLedger.open(sys.argv[1])
ledger.reserve_repair('easy-config', 'fixed_pro_high', 1, **json.loads(sys.argv[2]))
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", script, str(self.path), json.dumps(PRO)],
                       cwd=ROOT, check=True, timeout=15)
        self.ledger = PilotLedger.open(self.path)
        with self.assertRaisesRegex(LedgerRefused, "already reserved"):
            self.reserve()
        self.assertEqual(self.ledger.summary()["charged_usd_micros"], 156_162)

    def test_resume_requires_exact_private_initialized_ledger_and_frozen_config(self):
        with self.assertRaises(FileExistsError):
            PilotLedger.create(self.path)
        missing = self.path.with_name("missing.sqlite")
        with self.assertRaises(FileNotFoundError):
            PilotLedger.open(missing)
        self.assertFalse(missing.exists())
        self.path.chmod(0o644)
        with self.assertRaisesRegex(LedgerRefused, "owner-only"):
            PilotLedger.open(self.path)
        self.path.chmod(0o600)
        alias = self.path.with_name("alias.sqlite")
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(LedgerRefused, "owner-only"):
            PilotLedger.open(alias)
        with patch("pilot_ledger.ROOT", self.path.parent):
            (self.path.parent / "pilot.json").write_bytes((ROOT / "pilot.json").read_bytes() + b"\n")
            (self.path.parent / "corpus.json").write_bytes((ROOT / "corpus.json").read_bytes())
            with self.assertRaisesRegex(LedgerRefused, "another run, plan or corpus"):
                PilotLedger.open(self.path)
        missing.touch(mode=0o600)
        with self.assertRaises(sqlite3.DatabaseError):
            PilotLedger.open(missing)


if __name__ == "__main__":
    unittest.main()
