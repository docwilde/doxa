"""Offline contract tests for the installed-host plugin proof command."""

from __future__ import annotations

import importlib.util
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugin-installed-host-proof.py"
spec = importlib.util.spec_from_file_location("plugin_installed_host_proof", SCRIPT)
assert spec is not None and spec.loader is not None
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
driver_spec = importlib.util.spec_from_file_location("installed_tui_proof", SCRIPT.with_name("plugin-installed-tui-proof.py"))
assert driver_spec is not None and driver_spec.loader is not None
driver = importlib.util.module_from_spec(driver_spec)
driver_spec.loader.exec_module(driver)


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

    def test_activation_requires_fresh_run_and_explicit_instance(self) -> None:
        with mock.patch.object(proof, "observe", side_effect=AssertionError("host probe ran")):
            with mock.patch("sys.argv", [str(SCRIPT), "--check", "--frontend", str(self.root / "doxa-rs"),
                                         "--activate"]):
                with self.assertRaisesRegex(proof.common.ProofError, "fresh --run"):
                    proof.main()

    def test_signed_activation_is_separate_from_receipt_and_rejects_changed_binding(self) -> None:
        key = self.root / "operator.pem"
        subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                       check=True, capture_output=True)
        key.chmod(0o600)
        public = subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
                                check=True, capture_output=True).stdout[-32:].hex()
        options = argparse.Namespace(operator_key=key, home=self.root, lifetime_seconds=20)
        binding = {"instance": {"uid":os.geteuid(), "pid":42, "start_ticks":456,
                   "boot_id":"a" * 36, "home_device":1, "home_inode":2},
                   "policy":{"operator_public_key":public,"epoch":3}, "config_sha256":"c" * 64}
        identity = {"device":1,"inode":2,"bytes":3,"sha256":"a" * 64}
        installed = {"cgroup":{"parent":"/sys/fs/cgroup/delegated", "parent_device":1,
                     "parent_inode":2,"supervisor_device":1,"supervisor_inode":3},
                     **{role:identity for role in ("frontend","worker","bwrap")}}
        cases = {name:{} for name in proof.common.PROOF_CASES | proof.EXTRA_CASES.keys()}
        with mock.patch.object(proof, "activation_binding", return_value=binding):
            proof.sign_activation(options,binding,installed,cases,b"fresh-nine-case-log")
        permit_path = self.root / "native-plugin-authority/42-456.json"
        self.assertEqual(permit_path.stat().st_mode & 0o777,0o600)
        envelope = json.loads(permit_path.read_bytes())
        payload = bytes.fromhex(envelope["payload_hex"])
        permit = json.loads(payload)
        self.assertEqual(permit["purpose"],proof.AUTHORITY_PURPOSE)
        self.assertEqual(permit["instance"],binding["instance"])
        self.assertEqual(permit["epoch"],3)
        self.assertEqual(permit["cases"], sorted(cases))
        self.assertEqual(permit["expires_at"] - permit["issued_at"],20)
        self.assertEqual(permit["expires_boottime_ms"] - permit["issued_boottime_ms"],20000)
        pubpath = self.root / "public.pem"
        subprocess.run(["/usr/bin/openssl","pkey","-in",str(key),"-pubout","-out",str(pubpath)],
                       capture_output=True,check=True)
        payloadpath = self.root / "payload"
        signaturepath = self.root / "signature"
        payloadpath.write_bytes(payload)
        signaturepath.write_bytes(bytes.fromhex(envelope["signature_hex"]))
        verification = subprocess.run(["/usr/bin/openssl","pkeyutl","-verify","-rawin","-pubin",
                         "-inkey",str(pubpath),"-in",str(payloadpath),"-sigfile",str(signaturepath)],
                         capture_output=True)
        self.assertEqual(verification.returncode,0)
        with mock.patch.object(proof,"activation_binding", return_value={**binding,"config_sha256":"changed"}):
            with self.assertRaisesRegex(proof.common.ProofError,"changed during proof"):
                proof.sign_activation(options,binding,installed,cases,b"old-log")
        with mock.patch.object(proof,"activation_binding", return_value=binding):
            with self.assertRaisesRegex(proof.common.ProofError,"all nine fresh"):
                proof.sign_activation(options,binding,installed,{"approved-wasm":{}},b"incomplete")
            options.lifetime_seconds=901
            with self.assertRaisesRegex(proof.common.ProofError,"1–900"):
                proof.sign_activation(options,binding,installed,cases,b"log")

    def test_private_operator_state_refuses_links_loose_mode_and_unbounded_data(self) -> None:
        path = self.root / "state"
        path.write_bytes(b"key-state")
        path.chmod(0o600)
        self.assertEqual(proof.private_read(path),b"key-state")
        path.chmod(0o644)
        with self.assertRaises(proof.common.ProofError):
            proof.private_read(path)
        path.chmod(0o600)
        alias = self.root / "alias"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            proof.private_read(alias)
        with self.assertRaises(proof.common.ProofError):
            proof.private_read(path,1)

    def test_tui_screen_reconstructs_incremental_result_including_retained_letters(self) -> None:
        screen = driver.Screen(3,80)
        previous = "Checking exact zero-grant approval and installed-host admission"
        expected = "Run refused: package preflight failed"
        screen.feed(previous.encode())
        for column, character in enumerate(expected.ljust(len(previous))):
            if character != previous[column]:
                # ratatui omits cells whose letters already match. Stripping
                # these cursor moves would produce 'refusd' and 'filed'.
                screen.feed(f"\x1b[1;{column+1}H{character}".encode())
        self.assertIn(expected,screen.text())
        utf8 = "\x1b[2;1H─".encode()
        for byte in utf8:
            screen.feed(bytes([byte]))
        self.assertIn("─",screen.text())
        screen.feed(b"\x1b[1;1H\x1b[2K")
        self.assertNotIn(expected,screen.text())

    def test_operator_reads_allow_atime_updates_but_reject_content_metadata_changes(self) -> None:
        path = self.root / "private"
        path.write_bytes(b"operator-state")
        path.chmod(0o600)
        actual = path.stat()
        fields = {key:getattr(actual,key) for key in ("st_dev","st_ino","st_uid","st_mode","st_nlink",
                  "st_size","st_mtime_ns","st_ctime_ns")}
        before = SimpleNamespace(**fields,st_atime_ns=1)
        after = SimpleNamespace(**fields,st_atime_ns=2)
        with mock.patch.object(proof.os,"fstat",side_effect=(before,after)), mock.patch.object(Path,"lstat",return_value=after):
            self.assertEqual(proof.private_read(path),b"operator-state")
        changed = SimpleNamespace(**{**fields,"st_mtime_ns":fields["st_mtime_ns"]+1},st_atime_ns=2)
        with mock.patch.object(proof.os,"fstat",side_effect=(before,changed)), mock.patch.object(Path,"lstat",return_value=changed):
            with self.assertRaisesRegex(proof.common.ProofError,"changed during read"):
                proof.private_read(path)

    def test_tui_source_is_pinned_across_all_activations_and_receipt(self) -> None:
        pinned = {"commit":"a" * 40,"tree":"b" * 40}
        with mock.patch.object(driver.common,"source_identity",return_value=pinned):
            driver.require_source(pinned)
        for changed in ({**pinned,"commit":"c" * 40},{**pinned,"tree":"d" * 40}):
            with mock.patch.object(driver.common,"source_identity",return_value=changed):
                with self.assertRaisesRegex(driver.common.ProofError,"source changed"):
                    driver.require_source(pinned)


if __name__ == "__main__":
    unittest.main()
