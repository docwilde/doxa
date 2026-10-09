#!/usr/bin/env python3
"""Task-local, read-only regressions for the quota operator preflight."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from check_docker_quota_preflight import inspect, main, mount_for, parse_mountinfo


class QuotaPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        # The suite must never fall back to /tmp or a live DOXA session tree.
        self.scratch = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "session"
        self.root.mkdir(mode=0o700)
        for name in ("checkout", "home", "cache"):
            (self.root / name).mkdir(mode=0o700)

    @staticmethod
    def mountinfo(options: str = "rw,prjquota", extra: str = "") -> str:
        return f"1 0 8:1 / / rw - ext4 /dev/fixture {options}\n{extra}"

    @staticmethod
    def project(_fd: int) -> tuple[int, bool]:
        return 42, True

    def test_candidate_still_refuses_hard_quota_admission(self) -> None:
        before = sorted(p.name for p in self.root.iterdir())
        report = inspect(self.root, self.mountinfo(), self.project)
        self.assertEqual(report["status"], "candidate_unverified")
        self.assertTrue(report["capability_candidate"])
        self.assertFalse(report["hard_enforcement_verified"])
        self.assertFalse(report["admissible_as_hard_quota"])
        self.assertEqual({row["project_id"] for row in report["sources"].values()}, {42})
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_missing_mount_option_and_project_inheritance_are_unknown(self) -> None:
        report = inspect(self.root, self.mountinfo("rw"), lambda _fd: (42, False))
        self.assertEqual(report["status"], "unsupported")
        self.assertIn("project-quota mount option is not visible", report["reasons"])
        self.assertTrue(any("lacks a nonzero inherited project ID" in value for value in report["reasons"]))

    def test_different_project_ids_reject_candidate(self) -> None:
        def projects(fd: int) -> tuple[int, bool]:
            return (43 if os.readlink(f"/proc/self/fd/{fd}").endswith("/cache") else 42), True
        report = inspect(self.root, self.mountinfo(), projects)
        self.assertIn("private bind sources have different project IDs", report["reasons"])
        self.assertFalse(report["capability_candidate"])

    def test_unavailable_project_ioctl_is_not_treated_as_zero_safe_usage(self) -> None:
        def unavailable(_fd: int) -> tuple[int, bool]:
            raise OSError("fixture ioctl unavailable")
        report = inspect(self.root, self.mountinfo(), unavailable)
        self.assertFalse(report["capability_candidate"])
        self.assertTrue(any("project metadata is unavailable" in value for value in report["reasons"]))

    def test_symlink_and_nonprivate_source_refuse_inspection(self) -> None:
        (self.root / "cache").rmdir()
        (self.root / "cache").symlink_to(self.root / "home", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            inspect(self.root, self.mountinfo(), self.project)
        (self.root / "cache").unlink()
        (self.root / "cache").mkdir(mode=0o750)
        with self.assertRaisesRegex(ValueError, "mode 0700"):
            inspect(self.root, self.mountinfo(), self.project)

    def test_nested_host_mount_refuses_candidate_even_on_same_filesystem(self) -> None:
        nested = self.root / "checkout" / "nested"
        extra = f"2 1 8:1 /other {nested} rw - ext4 /dev/fixture rw,prjquota\n"
        report = inspect(self.root, self.mountinfo(extra=extra), self.project)
        self.assertIn("nested host mount exists under the private session tree", report["reasons"])

    def test_mount_parser_rejects_ambiguity_and_decodes_escaped_paths(self) -> None:
        mounts = parse_mountinfo("1 0 8:1 / / rw - ext4 /dev/fixture rw,prjquota\n"
                                 "2 1 8:1 /some /space\\040name rw - ext4 /dev/fixture rw,prjquota\n")
        self.assertEqual(mount_for(Path("/space name/x"), mounts).id, 2)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            mount_for(Path("/space name/x"), mounts + [mounts[1]])
        with self.assertRaisesRegex(ValueError, "invalid mountinfo"):
            parse_mountinfo("malformed row")

    def test_real_host_inspection_always_exits_nonzero_and_writes_nothing(self) -> None:
        before = sorted(p.name for p in self.root.iterdir())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main([str(self.root)])
        report = json.loads(output.getvalue())
        self.assertIn(exit_code, (2, 3))
        self.assertFalse(report["hard_enforcement_verified"])
        self.assertFalse(report["admissible_as_hard_quota"])
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()
