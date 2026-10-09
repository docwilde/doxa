"""Pure prerequisite checks for the opt-in plugin cgroup host proof."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
