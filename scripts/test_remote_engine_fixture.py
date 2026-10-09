#!/usr/bin/env python3
"""No-network regressions for remote Engine fixture gating."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from check_remote_engine_fixture import evaluate, load_private_fixture, main


def complete_fixture() -> dict:
    root = "/fixture/remote/session-1"
    names = (("checkout", "/workspace", True), ("home", "/home/doxa", True),
             ("cache", "/work-cache", True), ("broker", "/run/doxa/session", False))
    return {
        "version": 1, "kind": "remote-linux", "session_id": "session-1",
        "daemon_session_root": root, "daemon_uid": 1000, "worker_uid_map_host_uid": 1000,
        "engine_info": {"SecurityOptions": ["name=rootless"], "CgroupVersion": "2",
                        "CgroupDriver": "systemd", "MemoryLimit": True,
                        "CpuCfsQuota": True, "PidsLimit": True},
        "mounts": [{"name": name, "source": f"{root}/{name}", "target": target,
                    "writable": writable, "owner_uid": 1000, "mode": "0700",
                    "canonical": True, "symlink_free": True} for name, target, writable in names],
        "broker": {"transport": "authenticated-session-forward", "bound_session_id": "session-1",
                   "owner_uid": 1000, "mode": "0700", "exclusive": True,
                   "capability_bound": True, "worker_engine_socket_exposed": False},
        "requested": {"memory_bytes": 536870912, "cpus": 1.5, "pids": 128},
        "effective": {"private_cgroup_namespace": True, "memory_max": "536870912",
                      "memory_swap_max": "0", "cpu_max": "150000 100000", "pids_max": "128",
                      "writable_mount_probe": {"checkout": True, "home": True, "cache": True},
                      "broker_handshake_bound": True},
    }


class RemoteEngineFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.scratch.cleanup)
        self.path = Path(self.scratch.name) / "engine.json"

    def write(self, fixture: dict, mode: int = 0o600) -> None:
        self.path.write_text(json.dumps(fixture))
        self.path.chmod(mode)

    def test_complete_fixture_is_still_not_admissible(self) -> None:
        fixture = complete_fixture()
        report = evaluate(fixture)
        self.assertEqual(report["status"], "fixture_complete_untrusted")
        self.assertEqual(report["failed_gates"], [])
        self.assertFalse(report["production_admissible"])
        self.write(fixture)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([str(self.path)]), 3)
        self.assertFalse(json.loads(output.getvalue())["production_admissible"])
        fixture["kind"] = "docker-desktop"
        self.assertEqual(evaluate(fixture)["status"], "fixture_complete_untrusted")
        self.assertFalse(evaluate(fixture)["production_admissible"])
        fixture["endpoint"] = "ssh://unapproved.invalid"
        self.assertEqual(evaluate(fixture)["status"], "blocked")

    def test_rootless_identity_requires_exact_option_and_matching_uid(self) -> None:
        for mutate in (
            lambda row: row["engine_info"].update(SecurityOptions=["name=rootlesskit"]),
            lambda row: row.update(worker_uid_map_host_uid=0),
            lambda row: row.update(daemon_uid=0),
        ):
            fixture = complete_fixture(); mutate(fixture)
            report = evaluate(fixture)
            self.assertFalse(report["fixture_gates_satisfied"])
            self.assertTrue(any("rootless" in reason for reason in report["failed_gates"]))

    def test_private_broker_and_exact_daemon_host_mounts_are_required(self) -> None:
        changes = (
            lambda row: row["broker"].update(worker_engine_socket_exposed=True),
            lambda row: row["broker"].update(bound_session_id="other"),
            lambda row: row["mounts"][0].update(source="/client/checkout"),
            lambda row: row["mounts"][1].update(owner_uid=0),
            lambda row: row["mounts"][3].update(writable=True),
        )
        for mutate in changes:
            fixture = complete_fixture(); mutate(fixture)
            report = evaluate(fixture)
            self.assertFalse(report["fixture_gates_satisfied"])
            self.assertFalse(report["production_admissible"])

    def test_worker_effective_limits_not_requested_flags_determine_candidate(self) -> None:
        changes = (
            lambda row: row["engine_info"].update(CgroupVersion="1"),
            lambda row: row["effective"].update(memory_max="max"),
            lambda row: row["effective"].update(memory_swap_max="1"),
            lambda row: row["effective"].update(cpu_max="max 100000"),
            lambda row: row["effective"].update(pids_max="129"),
            lambda row: row["effective"]["writable_mount_probe"].update(cache=False),
        )
        for mutate in changes:
            fixture = complete_fixture(); mutate(fixture)
            report = evaluate(fixture)
            self.assertFalse(report["fixture_gates_satisfied"])

    def test_docker_desktop_fixture_without_rootless_or_private_controls_blocks(self) -> None:
        fixture = complete_fixture()
        fixture["kind"] = "docker-desktop"
        fixture["engine_info"]["SecurityOptions"] = ["name=seccomp,profile=builtin"]
        fixture["effective"]["private_cgroup_namespace"] = False
        report = evaluate(fixture)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["production_admissible"])

    def test_untrusted_file_must_be_private_bounded_and_task_local(self) -> None:
        self.write(complete_fixture(), 0o644)
        with self.assertRaisesRegex(ValueError, "owner-private"):
            load_private_fixture(self.path)
        self.path.chmod(0o600)
        self.assertEqual(load_private_fixture(self.path)["version"], 1)
        alias = self.path.parent / "alias.json"
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "canonical"):
            load_private_fixture(alias)
        self.path.write_bytes(b" " * (64 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            load_private_fixture(self.path)

    def test_malformed_fixture_blocks_without_network_access(self) -> None:
        for payload in ("not json", '{"version":1,"version":1}', '{"version":NaN}'):
            self.path.write_text(payload)
            self.path.chmod(0o600)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main([str(self.path)]), 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
