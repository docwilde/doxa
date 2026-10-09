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
              mock.patch.object(quota, "_owned_container_id", return_value="b" * 64),
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
              mock.patch.object(quota, "_owned_container_id", return_value="b" * 64),
              mock.patch.object(quota, "_docker", side_effect=fake_docker)):
            with self.assertRaisesRegex(ValueError, "did not prove bounded EDQUOT"):
                quota.probe(self.root, "unix:///run/user/1000/fixture.sock",
                            "sha256:" + "a" * 64, 1)
        self.assertEqual([args[0] for args in invocations], ["run", "rm"])

    def test_aggregate_restart_retains_three_binds_and_checks_edquot_again(self) -> None:
        token = "a" * 32
        name = "doxa-quota-probe-" + token
        prefix = 4 * 1024 * 1024
        third = 8 * 1024 * 1024
        invocations = []

        def fake_docker(_endpoint, args, _config, timeout=30, max_output=8192):
            invocations.append(args[0])
            if args[0] == "create":
                self.assertEqual(args.count("--mount"), 3)
                self.assertIn("none", args)
                return "b" * 64
            if args[0] in {"start", "stop"}:
                return ""
            self.assertEqual(args[0], "exec")
            mode, source, _cap, worker_token = args[-4:]
            self.assertEqual(worker_token, token)
            if mode == "check":
                return json.dumps({"sizes": {"checkout": prefix, "home": prefix,
                                                  "cache": third}})
            size = prefix if source != "cache" else (third if mode == "create" else 0)
            if mode == "create":
                marker = self.root / source / (".doxa-quota-probe-" + token)
                marker.write_bytes(b"")
                os.truncate(marker, size)
            return json.dumps({"source": source, "bytes_written": size,
                               "errno": errno.EDQUOT if source == "cache" else None})

        with (mock.patch.object(quota, "_docker", side_effect=fake_docker),
              mock.patch.object(quota, "_inspect_fixture_container") as inspect):
            result = quota._aggregate_restart_probe(
                self.root, "unix:///run/user/1000/fixture.sock", "sha256:" + "a" * 64,
                name, self.root, token, 32 * 1024 * 1024,
                {"checkout": 16 * 1024 * 1024, "home": 16 * 1024 * 1024,
                 "cache": 16 * 1024 * 1024})
        self.assertEqual(result["prefix_bytes_each"], prefix)
        self.assertEqual(result["third_bind_bytes_before_edquot"], third)
        self.assertEqual(result["additional_bytes_before_edquot_after_restart"], 0)
        self.assertEqual(invocations, ["create", "start", "exec", "exec", "exec",
                                       "stop", "start", "exec", "exec"])
        self.assertEqual(inspect.call_count, 2)

    def test_opt_in_aggregate_result_keeps_production_admission_closed(self) -> None:
        def fake_docker(_endpoint, args, _config, timeout=30):
            if args[0] == "rm":
                return ""
            self.assertEqual(args[0], "run")
            return json.dumps({"source": args[-3], "bytes_written": 1024,
                               "errno": errno.EDQUOT, "edquot": True})

        aggregate = {"prefix_bytes_each": 1024 * 1024,
                     "third_bind_bytes_before_edquot": 2048,
                     "additional_bytes_before_edquot_after_restart": 0,
                     "container_id": "b" * 64}
        with (mock.patch.object(quota, "_checked_socket"),
              mock.patch.object(quota, "_checked_fixture", return_value=(self.root, self.candidate)),
              mock.patch.object(quota, "_checked_engine"),
              mock.patch.object(quota, "_owned_container_id", return_value="b" * 64),
              mock.patch.object(quota, "_aggregate_restart_probe", return_value=aggregate) as run,
              mock.patch.object(quota, "_docker", side_effect=fake_docker)):
            receipt = quota.probe(self.root, "unix:///run/user/1000/fixture.sock",
                                  "sha256:" + "a" * 64, 16, aggregate_restart=True)
        run.assert_called_once()
        self.assertTrue(receipt["aggregate_restart_verified_for_fixture"])
        self.assertEqual(receipt["aggregate_restart_evidence"], aggregate)
        self.assertFalse(receipt["admissible_as_hard_quota"])

    def test_aggregate_receipts_refuse_per_bind_limit_and_enospc(self) -> None:
        good = {"source": "cache", "bytes_written": 4096, "errno": errno.EDQUOT}
        self.assertEqual(quota.validate_aggregate_receipt(good, "cache", 8192, True), 4096)
        for changed in ({"errno": errno.ENOSPC}, {"bytes_written": 8192},
                        {"bytes_written": -1}, {"bytes_written": True},
                        {"source": "home"}, {"extra": 1}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                quota.validate_aggregate_receipt(good | changed, "cache", 8192, True)

    def test_inspect_refuses_extra_or_changed_bind_and_open_network(self) -> None:
        image = "sha256:" + "a" * 64
        name = "doxa-quota-probe-test"
        mounts = [{"Source": str(self.root / source), "Destination": "/fixture/" + source,
                   "Type": "bind", "RW": True} for source in ("checkout", "home", "cache")]
        row = {"Id": "b" * 64, "Name": "/" + name, "Config": {"Image": image},
               "State": {"Running": True},
               "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True,
                              "Privileged": False, "CapDrop": ["ALL"]},
               "Mounts": mounts}
        row["Config"]["Labels"] = {"org.doxa.quota-probe": "token"}
        with mock.patch.object(quota, "_docker", return_value=json.dumps([row])):
            quota._inspect_fixture_container("fixture", name, self.root, image,
                                             "token", "b" * 64, self.root)
        for changed in ({"Mounts": mounts + [mounts[0]]},
                        {"Mounts": [mounts[0] | {"RW": False}, *mounts[1:]]},
                        {"HostConfig": row["HostConfig"] | {"NetworkMode": "bridge"}}):
            with (self.subTest(changed=changed),
                  mock.patch.object(quota, "_docker", return_value=json.dumps([row | changed])),
                  self.assertRaisesRegex(ValueError, "differ")):
                quota._inspect_fixture_container("fixture", name, self.root, image,
                                                 "token", "b" * 64, self.root)

    def test_cleanup_identity_refuses_name_collision(self) -> None:
        name = "doxa-quota-probe-owner"
        row = {"Id": "a" * 64, "Name": "/" + name,
               "Config": {"Labels": {"org.doxa.quota-probe": "other-token"}}}
        with mock.patch.object(quota, "_docker", side_effect=lambda *a, **k: json.dumps([row])):
            with self.assertRaisesRegex(ValueError, "refusing to remove"):
                quota._owned_container_id("fixture", name, "my-token", self.root)
            row["Config"]["Labels"]["org.doxa.quota-probe"] = "my-token"
            self.assertEqual(quota._owned_container_id("fixture", name, "my-token", self.root),
                             "a" * 64)

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
