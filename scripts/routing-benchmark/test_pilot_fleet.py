"""Exercise fleet CLI preflight and both ledger envelopes without dispatch."""
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from pilot_ledger import LedgerRefused, PilotLedger, micros, policy, token_cost

ROOT = Path(__file__).resolve().parents[2]


class FleetPilotFixture(unittest.TestCase):
    def test_dry_run_profiles_fit_two_retained_fleet_envelopes(self):
        override = os.environ.get("DOXA_PILOT_FIXTURE_BINARY")
        binary = Path(override or ROOT / "target/debug/doxa-rs").absolute()
        if override is None and not binary.is_file():
            self.skipTest("Build doxa-rs or set DOXA_PILOT_FIXTURE_BINARY to test fleet preflight")
        self.assertTrue(binary.is_file(), "fleet fixture requires a built doxa-rs binary")

        plan, _, _ = policy()
        flash = next(row for row in plan["candidates"] if row["model"] == "deepseek-flash")
        input_rate = micros(flash["input_usd_per_million"])
        output_rate = micros(flash["output_usd_per_million"])
        # Use three equal API slot ceilings and leave one cent of envelope headroom.
        run_usd = Decimal("2.49")
        review_usd = Decimal("0.24")
        host_slots = 3
        per_slot_micros = micros((run_usd - review_usd) / host_slots)
        envelope_micros = micros(plan["usd_per_fleet"])
        output_limit = 4_096
        input_bound = 32_768
        request_count = 4
        worker_hold = token_cost(input_bound * request_count, output_limit, input_rate, output_rate)
        self.assertLess(worker_hold, per_slot_micros)
        self.assertLess(per_slot_micros * host_slots + micros(review_usd), envelope_micros)

        candidate = {
            "id": "flash", "provider": flash["provider"], "model": flash["model"], "effort": flash["effort"],
            "description": "Pinned direct API candidate for fixture preflight",
            "context_tokens": input_bound + output_limit, "max_output_tokens": output_limit,
            "max_tool_requests": request_count, "supports_tools": True,
            "input_usd_micros_per_million": input_rate, "output_usd_micros_per_million": output_rate,
        }
        config = {
            "version": 1, "jev_model": plan["router_model"], "criteria_version": "development-fleet-fixture",
            # Router configs require two IDs. Both refer to the same API target, and every slot pins flash.
            "candidates": [candidate, {**candidate, "id": "flash-alternate"}],
            "fallback_id": "flash", "confidence_threshold": 0.75, "max_calls": host_slots,
            "max_spend_usd_micros": host_slots, "max_input_bytes": 24_576, "deadline_ms": 1_000,
        }
        with tempfile.TemporaryDirectory(prefix="dp-") as temporary:
            root = Path(temporary).resolve()
            config_path = root / "router.json"
            source = json.dumps(config, sort_keys=True).encode()
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(source)
            state = root / "state"
            state.mkdir(mode=0o700)
            environment = {"PATH": "/usr/bin:/bin", "DOXA_HOME": str(state),
                           "DOXA_LORE": "0", "DOXA_MCP_ENABLED": "0"}
            ledger_path = root / "pilot.sqlite"
            ledger = PilotLedger.create(ledger_path)
            for slot in (1, 2):
                argv = ["fleet", "start", "--dry-run", "--pool", "router:flash", "--supervisor", "router:flash",
                        "--router-config", str(config_path), "--sessions", "2", "--run-budget", str(run_usd),
                        "--review-budget", str(review_usd), "--isolation", "docker-offline",
                        "--alignment-supervisor", "deepseek:deepseek-flash", "--supervision-mode", "shadow",
                        "--message-review", "shadow", "--message-judge", "llm:deepseek:deepseek-flash",
                        "--review-input-price", flash["input_usd_per_million"],
                        "--review-output-price", flash["output_usd_per_million"],
                        "--review-max-calls", "6", "--approve", "peer", "--root", str(root / "f"),
                        "--run-id", f"fixture-{slot}", "--cwd", str(root), "--prompt", "Offline preflight fixture"]
                result = subprocess.run([str(binary), *argv], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual([line.split()[2] for line in lines if line.startswith("slot ")],
                                 ["router:flash"] * host_slots)
                access_line = next(line for line in lines if line.startswith("Access review: "))
                access = json.loads(access_line.removeprefix("Access review: "))
                self.assertEqual(access["worker_isolation"], "docker-offline")
                self.assertEqual(access["supervisor_isolation"], "docker-offline")
                self.assertFalse(access["worker_task_internet"])
                self.assertFalse(access["supervisor_task_internet"])
                self.assertFalse((root / "f").exists(), "dry run must not create a fleet runtime")
                fixture_identity = {"argv": argv, "router_sha256": hashlib.sha256(source).hexdigest()}
                digest = hashlib.sha256(json.dumps(fixture_identity, sort_keys=True).encode()).hexdigest()
                reservation = ledger.reserve_fleet(slot, ceiling_usd_micros=envelope_micros, configuration_sha256=digest)
                self.assertEqual(reservation.held_usd_micros, envelope_micros)
                ledger = PilotLedger.open(ledger_path)
                with self.assertRaisesRegex(LedgerRefused, "already reserved"):
                    ledger.reserve_fleet(slot, ceiling_usd_micros=envelope_micros, configuration_sha256=digest)
            unusable = argv.copy()
            unusable[unusable.index("--review-budget") + 1] = str(run_usd)
            result = subprocess.run([str(binary), *unusable], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("review budget must leave a positive worker budget", result.stderr)
            usage = ledger.summary()["allocations"]["fleet_development"]
            self.assertEqual(usage["unresolved"], 2)
            self.assertEqual(usage["charged_usd_micros"], 5_000_000)
            self.assertEqual(usage["available_usd_micros"], 0)


if __name__ == "__main__":
    unittest.main()
