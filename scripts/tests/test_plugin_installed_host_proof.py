"""Offline contract tests for the installed-host plugin proof command."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugin-installed-host-proof.py"
spec = importlib.util.spec_from_file_location("plugin_installed_host_proof", SCRIPT)
assert spec is not None and spec.loader is not None
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


class InstalledProofTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_trusted_identity_requires_private_stable_singly_linked_file(self) -> None:
        path = self.root / "doxa-rs"
        path.write_bytes(b"installed-frontend")
        path.chmod(0o700)
        identity = proof.trusted_identity(path)
        self.assertEqual(identity["bytes"], len(b"installed-frontend"))
        self.assertEqual(len(identity["sha256"]), 64)
        alias = self.root / "alias"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            proof.trusted_identity(alias)
        hardlink = self.root / "hardlink"
        os.link(path, hardlink)
        with self.assertRaises(proof.common.ProofError):
            proof.trusted_identity(path)
        hardlink.unlink()
        path.chmod(0o722)
        with self.assertRaises(proof.common.ProofError):
            proof.trusted_identity(path)
        path.chmod(0o700)
        with path.open("r+b") as stream:
            stream.truncate(proof.MAX_EXECUTABLE_BYTES + 1)
        with self.assertRaises(proof.common.ProofError):
            proof.trusted_identity(path)

    def test_host_report_requires_exact_non_authorizing_contract(self) -> None:
        digest = "a" * 64
        rows = [proof.HOST_HEADER,
                "delegated_parent=/sys/fs/cgroup/delegated device=1 inode=2",
                "supervisor_device=1 supervisor_inode=3",
                f"frontend device=4 inode=5 bytes=6 sha256={digest}",
                f"worker device=4 inode=7 bytes=8 sha256={digest}",
                f"bwrap device=4 inode=9 bytes=10 sha256={digest}", proof.HOST_FOOTER]
        observed = proof.parse_host_report(("\n".join(rows) + "\n").encode())
        self.assertEqual(observed["cgroup"]["parent_inode"], 2)
        self.assertEqual(observed["worker"]["sha256"], digest)
        for changed in (rows[:-1], rows + ["extra"],
                        [row.replace("TUI execution disabled", "TUI execution enabled") for row in rows],
                        [row.replace("sha256=" + digest, "sha256=bad") if row.startswith("worker ") else row
                         for row in rows]):
            with self.assertRaises(proof.common.ProofError):
                proof.parse_host_report(("\n".join(changed) + "\n").encode())

    def test_installed_log_requires_all_seven_containment_and_two_error_cases(self) -> None:
        names = ["approved-wasm", "boundary", "pids", "memory", "cpu", "setsid-cancel", "timeout"]
        outcomes = ["Return(17)", "Exit(0)", "Cancelled", "Crash(9)",
                    "Timeout", "Cancelled", "Timeout"]
        rows = []
        for name, outcome in zip(names, outcomes):
            prefix = ("test native_plugins::runner_sandbox::acceptance::installed_host_lifecycle_acceptance ... "
                      if name == "approved-wasm" else "")
            if name == "approved-wasm":
                rows.append(f"{prefix}plugin-acceptance case={name} outcome={outcome} elapsed_ms=2 "
                            "stale_approval=refused cleanup=removed")
            else:
                rows.append(f"plugin-acceptance case={name} outcome={outcome} elapsed_ms=2 "
                            "memory_peak=1 oom_kill=0 pids_peak=1 pids_max=0 "
                            "cpu_usec=1 cpu_throttled=0 stdout_bytes=0 stderr_bytes=0 cleanup=removed")
        rows.extend(["plugin-acceptance case=module-error outcome=ModuleFailure(Trap) elapsed_ms=2 cleanup=removed",
                     "plugin-acceptance case=launch-error outcome=Refused elapsed_ms=2 cleanup=removed"])
        suffix = "\ntest result: ok. 1 passed; 0 failed; 0 ignored\n"
        self.assertEqual(len(proof.installed_cases(("\n".join(rows) + suffix).encode())), 9)
        for changed in (rows[:-1], rows + [rows[-1]],
                        [row.replace("cleanup=removed", "cleanup=leaked") if "module-error" in row else row for row in rows],
                        [row.replace("outcome=Refused", "outcome=Return(0)") for row in rows]):
            with self.assertRaises(proof.common.ProofError):
                proof.installed_cases(("\n".join(changed) + suffix).encode())

    def test_run_rejects_missing_operator_go_and_digests_before_host_probe(self) -> None:
        frontend = self.root / "doxa-rs"
        with mock.patch.object(proof, "observe", side_effect=AssertionError("host probe ran")):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("sys.argv", [str(SCRIPT), "--run", "--frontend", str(frontend)]):
                    with self.assertRaises(proof.common.ProofError):
                        proof.main()
            with mock.patch.dict(os.environ, {"DOXA_PLUGIN_INSTALLED_HOST_ACCEPTANCE": "1",
                                               "DOXA_PLUGIN_OPERATOR_GO": "1"}, clear=True):
                with mock.patch.object(proof.common, "check_target_dir", return_value=self.root):
                    with mock.patch("sys.argv", [str(SCRIPT), "--run", "--frontend", str(frontend),
                                                 "--receipt", "proof.json"]):
                        with self.assertRaises(proof.common.ProofError):
                            proof.main()

    def test_reviewed_digest_mismatch_is_rejected(self) -> None:
        reviewed = "a" * 64
        options = mock.Mock(expected_frontend_sha256=reviewed,
                            expected_worker_sha256=reviewed,
                            expected_bwrap_sha256=reviewed)
        installed = {role: {"sha256": reviewed} for role in ("frontend", "worker", "bwrap")}
        proof.expected_digests(options, installed)
        installed["worker"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(proof.common.ProofError, "worker digest differs"):
            proof.expected_digests(options, installed)

    def test_process_started_hook_reports_exact_bounded_runner_pid(self) -> None:
        pids: list[int] = []
        status, output = proof.common.run_bounded(
            [sys.executable, "-c", "print('ready')"], cwd=self.root,
            environment=os.environ.copy(), limit=1024, timeout=3,
            process_started=pids.append)
        self.assertEqual((status, output), (0, b"ready\n"))
        self.assertEqual(len(pids), 1)
        self.assertGreater(pids[0], 0)


if __name__ == "__main__":
    unittest.main()
