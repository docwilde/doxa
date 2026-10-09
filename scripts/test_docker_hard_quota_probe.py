#!/usr/bin/env python3
"""No live Docker or quota writes: check the opt-in probe's refusal paths."""

from __future__ import annotations

import errno
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import probe_docker_hard_quota as quota


class HardQuotaProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"
        self.root.mkdir(mode=0o700)
        for name in ("checkout", "home", "cache"):
            (self.root / name).mkdir(mode=0o700)
        self.candidate = {"capability_candidate": True, "descendants_checked": 3,
                          "sources": {"root": {"project_id": 42}}}

    def test_receipt_requires_actual_edquot_and_bounded_positive_writes(self) -> None:
        good = {"source": "checkout", "bytes_written": 4096,
                "errno": errno.EDQUOT, "edquot": True}
        self.assertEqual(quota.validate_receipt(good, "checkout", 8192), 4096)
        for changes in (
            {"errno": errno.ENOSPC, "edquot": False},
            {"bytes_written": 8192},
            {"bytes_written": 0},
            {"source": "home"},
            {"edquot": False},
            {"bytes_written": True},
            {"extra": "unreviewed"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                quota.validate_receipt(good | changes, "checkout", 8192)

    def test_fixture_must_be_private_empty_and_under_real_disk_tmpdir(self) -> None:
        with mock.patch.object(quota, "inspect", return_value=self.candidate):
            self.assertEqual(quota._checked_fixture(self.root)[0], self.root)
            (self.root / "cache" / "existing.txt").write_text("fixture")
            with self.assertRaisesRegex(ValueError, "empty"):
                quota._checked_fixture(self.root)
            (self.root / "cache" / "existing.txt").unlink()
            alias = self.root.parent / "alias"
            alias.symlink_to(self.root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "canonical"):
                quota._checked_fixture(alias)
        with mock.patch.object(quota, "inspect", return_value={"capability_candidate": False,
                                                                "reasons": ["different project ID"]}):
            with self.assertRaisesRegex(ValueError, "different project ID"):
                quota._checked_fixture(self.root)

    def test_bounded_rootless_run_uses_three_binds_and_keeps_admission_false(self) -> None:
        invocations = []

        def fake_docker(_endpoint, args, _config, timeout=30):
            invocations.append((args, timeout))
            if args[0] == "rm":
                return ""
            self.assertEqual(args[0], "run")
            source = args[-3]
            return json.dumps({"source": source, "bytes_written": 1024,
                               "errno": errno.EDQUOT, "edquot": True})

        with (mock.patch.object(quota, "_checked_socket"),
              mock.patch.object(quota, "_checked_fixture", return_value=(self.root, self.candidate)),
              mock.patch.object(quota, "_checked_engine"),
              mock.patch.object(quota, "_docker", side_effect=fake_docker)):
            receipt = quota.probe(self.root, "unix:///run/user/1000/fixture.sock",
                                  "sha256:" + "a" * 64, 1)
        self.assertTrue(receipt["hard_enforcement_verified_for_fixture"])
        self.assertFalse(receipt["admissible_as_hard_quota"])
        self.assertEqual(set(receipt["bytes_before_edquot"]), {"checkout", "home", "cache"})
        self.assertEqual(len(invocations), 4)
        for args, timeout in invocations[:3]:
            self.assertEqual(timeout, 120)
            self.assertIn("none", args)
            self.assertIn("never", args)
            self.assertIn("--read-only", args)
            self.assertEqual(args.count("--mount"), 3)
        self.assertEqual(invocations[3][0][0:2], ["rm", "-f"])

    def test_missing_edquot_refuses_and_reaps_only_fixture_container(self) -> None:
        invocations = []

        def fake_docker(_endpoint, args, _config, timeout=30):
            invocations.append(args)
            if args[0] == "rm":
                return ""
            return json.dumps({"source": "checkout", "bytes_written": 1024 * 1024,
                               "errno": None, "edquot": False})

        with (mock.patch.object(quota, "_checked_socket"),
              mock.patch.object(quota, "_checked_fixture", return_value=(self.root, self.candidate)),
              mock.patch.object(quota, "_checked_engine"),
              mock.patch.object(quota, "_docker", side_effect=fake_docker)):
            with self.assertRaisesRegex(ValueError, "did not prove bounded EDQUOT"):
                quota.probe(self.root, "unix:///run/user/1000/fixture.sock",
                            "sha256:" + "a" * 64, 1)
        self.assertEqual([args[0] for args in invocations], ["run", "rm"])

    def test_rootful_endpoint_and_unpinned_image_refuse_before_docker(self) -> None:
        with self.assertRaisesRegex(ValueError, "task-local rootless"):
            quota._checked_socket("unix:///var/run/docker.sock")
        with mock.patch.object(quota, "_docker") as docker:
            with self.assertRaisesRegex(ValueError, "pinned"):
                quota._checked_engine("unix:///run/user/1000/fixture.sock", "python:latest", self.root)
            docker.assert_not_called()

    def test_explicit_acknowledgment_is_required_before_any_write(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            quota.main([str(self.root), "--docker-host", "unix:///run/user/1000/fixture.sock",
                        "--image", "sha256:" + "a" * 64])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
