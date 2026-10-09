"""Pure prerequisite checks for the opt-in plugin cgroup host proof."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
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
