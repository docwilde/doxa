"""Pure prerequisite checks for the opt-in plugin cgroup host proof."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "plugin-delegated-host-proof.py"
spec = importlib.util.spec_from_file_location("plugin_delegated_host_proof", SCRIPT)
proof = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(proof)


class DelegatedShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cgroup"
        self.parent = self.root / "delegated"
        self.leaf = self.parent / "supervisor"
        self.leaf.mkdir(parents=True)
        (self.parent / "cgroup.procs").write_text("")
        (self.parent / "cgroup.subtree_control").write_text("cpu memory pids\n")
        (self.leaf / "cgroup.procs").write_text("4242\n")
        self.membership = "0::/delegated/supervisor\n"
        self.uid = os.geteuid()

    def candidate(self, membership: str | None = None, uid: int | None = None) -> Path:
        return proof.delegated_parent(
            self.root, membership or self.membership,
            self.uid if uid is None else uid, 4242,
        )

    def test_accepts_empty_delegated_parent_with_direct_supervisor_leaf(self) -> None:
        self.assertEqual(self.candidate(), self.parent)

    def test_rejects_populated_or_disabled_parent(self) -> None:
        (self.parent / "cgroup.procs").write_text("7\n")
        with self.assertRaises(proof.ProofError):
            self.candidate()
        (self.parent / "cgroup.procs").write_text("")
        (self.parent / "cgroup.subtree_control").write_text("cpu pids\n")
        with self.assertRaises(proof.ProofError):
            self.candidate()

    def test_rejects_incorrect_owner_and_missing_caller(self) -> None:
        with self.assertRaises(proof.ProofError):
            self.candidate(uid=self.uid + 1)
        (self.leaf / "cgroup.procs").write_text("7\n")
        with self.assertRaises(proof.ProofError):
            self.candidate()

    def test_rejects_ambiguous_parent_traversal_and_symlink(self) -> None:
        for membership in (
            "0::/delegated/supervisor\n0::/other\n",
            "0::/delegated/../supervisor\n",
            "0::/delegated\n",
            "0::/\n",
        ):
            with self.subTest(membership=membership), self.assertRaises(proof.ProofError):
                self.candidate(membership)
        (self.root / "linked").symlink_to(self.parent)
        with self.assertRaises(proof.ProofError):
            self.candidate("0::/linked/supervisor\n")

    def test_requires_cleanup_and_counter_files_and_child_capacity(self) -> None:
        for name in ("cgroup.kill", "cgroup.events", "memory.peak",
                     "memory.swap.max", "pids.peak", "pids.events", "cpu.stat"):
            (self.parent / name).write_text("0\n")
        (self.parent / "cgroup.max.depth").write_text("max\n")
        (self.parent / "cgroup.max.descendants").write_text("1\n")
        proof.check_cgroup_files(self.parent)
        (self.parent / "cgroup.max.descendants").write_text("0\n")
        with self.assertRaises(proof.ProofError):
            proof.check_cgroup_files(self.parent)
        (self.parent / "cgroup.max.descendants").write_text("max\n")
        (self.parent / "cgroup.kill").unlink()
        with self.assertRaises(proof.ProofError):
            proof.check_cgroup_files(self.parent)

    def test_cgroup_receipt_binds_direct_supervisor_inode(self) -> None:
        observed = proof.cgroup_identity(self.parent, self.root, self.membership)
        self.assertEqual(observed["parent_inode"], self.parent.stat().st_ino)
        self.assertEqual(observed["supervisor_inode"], self.leaf.stat().st_ino)
        with self.assertRaises(proof.ProofError):
            proof.cgroup_identity(self.parent, self.root, "0::/other/supervisor\n")
        with self.assertRaises(proof.ProofError):
            proof.cgroup_identity(self.parent, self.root, "0::/delegated/../supervisor\n")

    def test_worker_cgroup_inventory_requires_no_leftovers(self) -> None:
        proof.require_no_plugin_cgroups(self.parent)
        (self.parent / "doxa-plugin-leak").mkdir()
        with self.assertRaises(proof.ProofError):
            proof.require_no_plugin_cgroups(self.parent)

    def test_source_identity_refuses_untracked_and_ignored_build_inputs(self) -> None:
        repo = Path(self.temp.name) / "source"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text("hidden-input\n")
        (repo / "source.rs").write_text("fn main() {}\n")
        subprocess.run(["git", "add", ".gitignore", "source.rs"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=Proof Test", "-c", "user.email=proof@example.invalid",
                        "commit", "-q", "-m", "test source"], cwd=repo, check=True)
        identity = proof.source_identity(repo)
        self.assertEqual(len(identity["commit"]), 40)
        (repo / "untracked.rs").write_text("unexpected")
        with self.assertRaises(proof.ProofError):
            proof.source_identity(repo)
        (repo / "untracked.rs").unlink()
        (repo / "hidden-input").write_text("unexpected")
        with self.assertRaises(proof.ProofError):
            proof.source_identity(repo)

    def test_run_requires_two_explicit_opt_ins_before_preflight(self) -> None:
        with mock.patch.object(proof, "preflight", side_effect=AssertionError("preflight ran")):
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch("sys.argv", [str(SCRIPT), "--run"]):
                    with self.assertRaises(proof.ProofError):
                        proof.main()
            with mock.patch.dict(os.environ, {"DOXA_PLUGIN_CGROUP_ACCEPTANCE": "1"}, clear=True):
                with mock.patch("sys.argv", [str(SCRIPT), "--run"]):
                    with self.assertRaises(proof.ProofError):
                        proof.main()
            with mock.patch.dict(os.environ, {
                "DOXA_PLUGIN_CGROUP_ACCEPTANCE": "1",
                "DOXA_PLUGIN_DISPOSABLE_HOST": "1",
            }, clear=True):
                with mock.patch("sys.argv", [str(SCRIPT), "--run"]):
                    with self.assertRaises(proof.ProofError):
                        proof.main()  # A real-disk CARGO_TARGET_DIR is also mandatory.

    def test_target_dir_rejects_tmpfs_paths_and_accepts_real_disk_parent(self) -> None:
        with self.assertRaises(proof.ProofError):
            proof.check_target_dir("/tmp/doxa-plugin-target")
        target = Path(self.temp.name) / "target"
        self.assertEqual(proof.check_target_dir(str(target)), target)

    def test_stages_independent_private_worker_from_hardlinked_cargo_output(self) -> None:
        source = Path(self.temp.name) / "doxa-plugin-worker"
        source.write_bytes(b"#!/bin/sh\nexit 0\n")
        source.chmod(0o755)
        os.link(source, Path(self.temp.name) / "cargo-hardlink")
        self.assertEqual(source.stat().st_nlink, 2)
        with proof.staged_worker(source, Path(self.temp.name)) as staged:
            metadata = staged.stat()
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
            self.assertEqual(metadata.st_uid, os.geteuid())
            self.assertNotEqual(metadata.st_ino, source.stat().st_ino)
            self.assertEqual(staged.read_bytes(), source.read_bytes())
        self.assertFalse(staged.exists())
        self.assertEqual(source.stat().st_nlink, 2)

    def test_staging_rejects_symlinked_worker(self) -> None:
        source = Path(self.temp.name) / "worker"
        source.write_bytes(b"worker")
        source.chmod(0o700)
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(source)
        with self.assertRaises(OSError):
            with proof.staged_worker(alias, Path(self.temp.name)):
                pass

    def test_executable_digest_rejects_symlink_replacement(self) -> None:
        source = Path(self.temp.name) / "worker"
        source.write_bytes(b"worker")
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(source)
        with self.assertRaises(OSError):
            proof.file_sha256(alias)

    def test_bounded_runner_kills_output_flood_before_unbounded_capture(self) -> None:
        start = time.monotonic()
        with self.assertRaisesRegex(proof.ProofError, "output limit"):
            proof.run_bounded([sys.executable, "-c", "import os,time; os.write(1,b'x'*1000000); time.sleep(30)"],
                              cwd=Path(self.temp.name), environment=os.environ.copy(),
                              limit=1024, timeout=3)
        self.assertLess(time.monotonic() - start, 3)

    def test_bounded_runner_deadline_kills_child_process_group(self) -> None:
        marker = Path(self.temp.name) / "child-survived"
        child = f"import time,pathlib; time.sleep(0.7); pathlib.Path({str(marker)!r}).write_text('bad')"
        parent = f"import subprocess,time,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); print('started',flush=True); time.sleep(30)"
        start = time.monotonic()
        with self.assertRaisesRegex(proof.ProofError, "deadline"):
            proof.run_bounded([sys.executable, "-c", parent], cwd=Path(self.temp.name),
                              environment=os.environ.copy(), limit=1024, timeout=0.25)
        self.assertLess(time.monotonic() - start, 2)
        time.sleep(0.8)
        self.assertFalse(marker.exists(), "child escaped proof process-group cleanup")

    def test_opened_test_executable_runs_exact_inode_after_path_swap(self) -> None:
        path = Path(self.temp.name) / "test-executable"
        shutil.copy2("/usr/bin/true", path)
        path.chmod(0o700)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as opened:
            expected = proof.descriptor_sha256(opened.fileno(), str(path))
            path.rename(path.with_name("reviewed-original"))
            shutil.copy2("/usr/bin/false", path)
            status, output = proof.run_bounded([str(path)], cwd=Path(self.temp.name),
                                               environment=os.environ.copy(), limit=1024,
                                               timeout=3, executable=f"/proc/self/fd/{opened.fileno()}",
                                               pass_fds=(opened.fileno(),))
            self.assertEqual((status, output), (0, b""))
            self.assertNotEqual(expected, proof.file_sha256(path))

    def test_cargo_artifact_parser_requires_one_in_target_lib_test(self) -> None:
        target = Path(self.temp.name) / "target"
        target.mkdir()
        executable = target / "doxa_tui-test"
        executable.write_bytes(b"fixture")
        row = {"reason": "compiler-artifact", "target": {"name": "doxa_tui", "kind": ["lib"]},
               "profile": {"test": True}, "executable": str(executable)}
        encoded = (json.dumps(row) + "\n").encode()
        self.assertEqual(proof.test_artifact(encoded, target), executable)
        for bad in (b"", encoded + encoded,
                    (json.dumps({**row, "executable": str(Path(self.temp.name) / "outside")}) + "\n").encode()):
            with self.assertRaises((proof.ProofError, FileNotFoundError)):
                proof.test_artifact(bad, target)

    def test_build_identity_changes_with_config_and_build_environment(self) -> None:
        root = Path(self.temp.name) / "source"
        (root / ".cargo").mkdir(parents=True)
        config = root / ".cargo/config.toml"
        config.write_text("[build]\njobs = 2\n")
        environment = {**os.environ, "CARGO_TARGET_DIR": str(Path(self.temp.name) / "target")}
        first = proof.build_identity(root, environment)
        config.write_text("[build]\njobs = 3\n")
        self.assertNotEqual(proof.build_identity(root, environment), first)
        config.write_text("[build]\njobs = 2\n")
        self.assertNotEqual(proof.build_identity(root, {**environment, "RUSTFLAGS": "-C opt-level=1"}), first)

    def test_receipt_is_private_exclusive_and_explicitly_non_authorizing(self) -> None:
        scratch = Path(self.temp.name)
        for bad in ("../escape", "/absolute", "UPPER", ".", "a/b"):
            with self.subTest(bad=bad), self.assertRaises(proof.ProofError):
                proof.receipt_name(bad, scratch)
        receipt = proof.receipt_name("proof.json", scratch)
        proof.write_receipt(receipt, {"tui_execution_authorized": False})
        self.assertEqual(json.loads(receipt.read_text()), {"tui_execution_authorized": False})
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            proof.write_receipt(receipt, {"tui_execution_authorized": True})
        self.assertEqual(json.loads(receipt.read_text()), {"tui_execution_authorized": False})

    def test_seven_case_log_refuses_missing_duplicate_and_failed_cleanup(self) -> None:
        names = ["approved-wasm", "boundary", "pids", "memory", "cpu", "setsid-cancel", "timeout"]
        outcomes = ["Return(17)", "Exit(0)", "Cancelled", "Crash(9)",
                    "Timeout", "Cancelled", "Timeout"]
        rows = []
        for name, outcome in zip(names, outcomes):
            if name == "approved-wasm":
                rows.append("test native_plugins::runner_sandbox::acceptance::delegated_cgroup_containment_acceptance ... "
                            f"plugin-acceptance case={name} outcome={outcome} elapsed_ms=2 "
                            "stale_approval=refused cleanup=removed")
            else:
                rows.append(f"plugin-acceptance case={name} outcome={outcome} elapsed_ms=2 "
                            "memory_peak=1 oom_kill=0 pids_peak=1 pids_max=0 "
                            "cpu_usec=1 cpu_throttled=0 stdout_bytes=0 stderr_bytes=0 cleanup=removed")
        suffix = "\ntest result: ok. 1 passed; 0 failed; 0 ignored\n"
        self.assertEqual(set(proof.proof_cases(("\n".join(rows) + suffix).encode())), set(names))
        for changed in (rows[:-1], rows + [rows[0]],
                        [row.replace("cleanup=removed", "cleanup=leaked") if "case=cpu " in row else row for row in rows],
                        [row.replace("stale_approval=refused", "stale_approval=accepted") for row in rows],
                        [row.replace("outcome=Exit(0)", "outcome=Exit(1)") for row in rows]):
            with self.assertRaises(proof.ProofError):
                proof.proof_cases(("\n".join(changed) + suffix).encode())
        with self.assertRaises(proof.ProofError):
            proof.proof_cases(("\n".join(rows)).encode())

    def test_namespace_receipt_requires_four_distinct_private_identities(self) -> None:
        host = {name: f"{name}:[100]" for name in proof.NAMESPACES}
        child = {name: f"{name}:[{number}]" for name, number in
                 zip(proof.NAMESPACES, (201, 202, 203, 204))}
        receipt = b"net=net:[201]\nmnt=mnt:[202]\nuser=user:[203]\npid=pid:[204]\n"
        proof.check_namespace_receipt(receipt, host)
        for name in proof.NAMESPACES:
            with self.subTest(reused=name), self.assertRaises(proof.ProofError):
                proof.check_namespace_receipt(receipt, {**host, name: child[name]})
        for bad in (
            b"net=net:[201]\nmnt=mnt:[202]\nuser=user:[203]\n",
            receipt + b"net=net:[205]\n",
            b"mnt=mnt:[202]\nnet=net:[201]\nuser=user:[203]\npid=pid:[204]\n",
            b"net=net:[x]\nmnt=mnt:[202]\nuser=user:[203]\npid=pid:[204]\n",
            receipt + b"\xff",
            b"x" * 1025,
        ):
            with self.subTest(bad=bad[:40]), self.assertRaises(proof.ProofError):
                proof.check_namespace_receipt(bad, host)

    def test_bwrap_smoke_requires_receipt_and_egress_checks(self) -> None:
        fake_bwrap = Path(self.temp.name) / "bwrap"
        fake_bwrap.write_bytes(b"#!/bin/sh\nexit 0\n")
        fake_bwrap.chmod(0o700)
        smoke_script = []
        receipt = "".join(f"{name}={name}:[999999999999]\n" for name in proof.NAMESPACES).encode()

        def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            if args[1] == "--help":
                return subprocess.CompletedProcess(args, 0, stdout=" ".join(proof.BWRAP_FLAGS))
            if args[1] == "--version":
                return subprocess.CompletedProcess(args, 0, stdout="bwrap fixture")
            smoke_script.append(args[-1])
            return subprocess.CompletedProcess(args, 0, stdout=receipt, stderr=b"")

        with mock.patch.object(proof, "BWRAP", fake_bwrap), mock.patch.object(
            proof.subprocess, "run", side_effect=fake_run
        ):
            self.assertEqual(proof.check_bwrap(), "bwrap fixture")
        self.assertEqual(len(smoke_script), 1)
        self.assertEqual(subprocess.run(["/bin/sh", "-n", "-c", smoke_script[0]]).returncode, 0)
        for source in ("/proc/net/dev", "/proc/net/route", "/proc/net/ipv6_route",
                       "/proc/self/ns/net", "/proc/self/ns/mnt", "/proc/self/ns/user",
                       "/proc/self/ns/pid"):
            self.assertIn(source, smoke_script[0])

    def test_route_policy_allows_loopback_and_denies_egress(self) -> None:
        ipv4_header = "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        ipv4_loopback = "lo 0000007F 00000000 0001 0 0 0 000000FF 0 0 0\n"
        ipv6_loopback = ("0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32
                         + " ffffffff 00000001 00000000 00200200 lo\n")
        route_file = Path(self.temp.name) / "routes"

        def accepted(policy: str, contents: str) -> bool:
            route_file.write_text(contents)
            return subprocess.run(["/usr/bin/awk", policy, str(route_file)],
                                  capture_output=True, check=False).returncode == 0

        self.assertTrue(accepted(proof.IPV4_ROUTE_POLICY, ipv4_header + ipv4_loopback))
        self.assertTrue(accepted(proof.IPV6_ROUTE_POLICY, ipv6_loopback))
        self.assertFalse(accepted(proof.IPV4_ROUTE_POLICY,
                                  ipv4_header + ipv4_loopback.replace("lo ", "eth0 ")))
        self.assertFalse(accepted(proof.IPV6_ROUTE_POLICY,
                                  ipv6_loopback.replace(" lo\n", " eth0\n")))
        self.assertFalse(accepted(proof.IPV4_ROUTE_POLICY, ipv4_header + "eth0\n"))
        self.assertFalse(accepted(proof.IPV6_ROUTE_POLICY, "malformed lo\n"))


if __name__ == "__main__":
    unittest.main()
