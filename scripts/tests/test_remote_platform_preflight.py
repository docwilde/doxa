# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free collector tests; these never prove real device delivery."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "verify_remote_platform_preflight.py"
SPEC = importlib.util.spec_from_file_location("remote_platform", SCRIPT)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


def host(identity="1", platform="linux"):
    return {"status": "observed", "platform": platform, "machine_hash": identity * 64,
            "tailscale_installed": True, "tailscale": {"online": True, "user_owned": True},
            "doxa_installed": True, "hub_installed": True, "claude_cli_installed": True}


class CollectorTests(unittest.TestCase):
    def test_prerequisites_never_close_release_gates(self):
        android = {"status": "observed", "physical_candidate": True, "boot_completed": True,
                   "play_services_installed": True, "tailscale_installed": True, "app_installed": True}
        result = qa.make_receipt(android, [host(), host("2", "darwin")])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(set(result["release_gates"].values()), {"unknown"})
        self.assertTrue(all(result["prerequisites"].values()))

    def test_duplicate_host_missing_identity_offline_and_tagged_are_not_two_hosts(self):
        for second in (host(), host() | {"machine_hash": None},
                       host("2") | {"tailscale": {"online": False, "user_owned": True}},
                       host("2") | {"tailscale": {"online": True, "user_owned": False}},
                       host("2") | {"doxa_installed": False}):
            with self.subTest(second=second):
                self.assertFalse(qa.make_receipt({}, [host(), second])["prerequisites"]["two_user_owned_tailnet_hosts"])

    def test_no_automatic_device_selection(self):
        calls = []
        def runner(command):
            calls.append(command)
            return "List of devices attached\nprivate-serial\tdevice\nother\tunauthorized"
        result = qa.android_observation("adb", None, runner)
        self.assertEqual(result["attached_devices"], 1)
        self.assertEqual(calls, [["adb", "devices"]])
        self.assertNotIn("private-serial", json.dumps(result))

    def device(self, serial="phone-1", qemu="0", failed=False):
        def runner(command):
            if failed:
                raise RuntimeError("private-fixture-secret")
            tail = command[3:]
            if tail == ["get-state"]:
                return "device"
            if tail == ["shell", "getprop", "ro.kernel.qemu"]:
                return qemu
            if tail == ["shell", "getprop", "sys.boot_completed"]:
                return "1"
            if tail == ["shell", "getprop", "ro.build.version.sdk"]:
                return "36"
            return "package:/private/fixture.apk"
        return qa.android_observation("adb", serial, runner)

    def test_emulator_or_unreadable_qemu_never_physical_candidate(self):
        for serial, qemu in (("emulator-5580", "0"), ("phone-1", "1"), ("phone-1", None)):
            self.assertFalse(self.device(serial, qemu)["physical_candidate"])
        result = self.device()
        self.assertTrue(result["physical_candidate"])
        self.assertNotIn("phone-1", json.dumps(result))
        self.assertNotIn("fixture.apk", json.dumps(result))

    def test_failed_or_unauthorized_device_is_unknown_without_raw_errors(self):
        result = self.device(failed=True)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("private-fixture-secret", json.dumps(result))

    def test_alias_and_serial_option_injection_refused_before_process(self):
        def forbidden(command):
            self.fail("unexpected process")
        for value in ("-oProxyCommand=x", "host;id", "$(id)", "host name"):
            self.assertEqual(qa.remote_observation(value, forbidden)["reason"], "invalid_ssh_alias")
            self.assertEqual(qa.android_observation("adb", value, forbidden)["reason"], "invalid_device_serial")

    def test_remote_reply_allowlist_rejects_extra_private_fields(self):
        value = host()
        value.pop("status")
        result = qa.remote_observation("test-host", lambda command: json.dumps(value))
        self.assertEqual(result["status"], "observed")
        for changed in (value | {"credential": "private-fixture-secret"},
                        value | {"machine_hash": "private-fixture-secret"},
                        value | {"tailscale": {"online": 1, "user_owned": True}},
                        {"error": "private-fixture-secret"}):
            result = qa.remote_observation("test-host", lambda command: json.dumps(changed))
            self.assertEqual(result["status"], "unknown")
            self.assertNotIn("private-fixture-secret", json.dumps(result))

    def test_ssh_uses_fixed_source_and_existing_host_key_without_install(self):
        calls = []
        value = host()
        value.pop("status")
        def runner(command):
            calls.append(command)
            return json.dumps(value)
        qa.remote_observation("test-host", runner)
        command = calls[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        words = shlex.split(command[-1])
        self.assertEqual(words[:2], ["python3", "-c"])
        # Compiles complete source; executing host command itself is avoided.
        compile(words[2], "remote-observer", "exec")
        self.assertIn("__name__ = 'doxa_remote_observer'", words[2])

    def test_tailscale_tags_and_invalid_values_fail_closed(self):
        for raw in (None, "bad", "[]", '{"Self":null}',
                    '{"BackendState":"Running","Self":{"Online":true,"UserID":true}}',
                    '{"BackendState":"Running","Self":{"Online":true,"UserID":1,"Tags":["tag:server"]}}'):
            self.assertFalse(qa.tailscale_state(raw)["user_owned"])
        self.assertEqual(qa.tailscale_state('{"BackendState":"Running","Self":{"Online":true,"UserID":1}}'),
                         {"online": True, "user_owned": True})

    def test_process_output_cap_and_deadline_kill_group(self):
        for code, timeout in (("print('x'*100000)", 2),
                              ("import time; time.sleep(20)", .1),
                              ("import sys; print('private-fixture-secret',file=sys.stderr); sys.exit(1)", 2)):
            started = time.monotonic()
            self.assertIsNone(qa.observe([sys.executable, "-c", code],
                lambda command: qa.bounded(command, timeout)))
            self.assertLess(time.monotonic() - started, 3)

    def test_completed_process_never_signals_a_reaped_group(self):
        with mock.patch.object(qa.os, "killpg", side_effect=AssertionError("reaped PGID signalled")):
            self.assertEqual(qa.bounded([sys.executable, "-c", "print('complete')"]), "complete")
            with self.assertRaises(ValueError):
                qa.bounded([sys.executable, "-c", "import sys; sys.exit(1)"])

    @unittest.skipUnless(hasattr(os, "fork") and hasattr(os, "waitid"), "requires POSIX waitid")
    def test_deadline_kills_pipe_holder_before_reaping_exited_leader(self):
        original_killpg = os.killpg
        observations = []
        def kill_reserved_group(pid, sig):
            # WNOWAIT observes without releasing the group leader's reserved PID.
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            observations.append((pid, sig, status.si_pid if status else None))
            original_killpg(pid, sig)
        code = "import os,time\nif os.fork() == 0:\n time.sleep(20)\nelse:\n os._exit(0)"
        started = time.monotonic()
        with mock.patch.object(qa.os, "killpg", side_effect=kill_reserved_group):
            with self.assertRaises(TimeoutError):
                qa.bounded([sys.executable, "-c", code], timeout=.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(observations), 1)
        pid, sig, observed_pid = observations[0]
        self.assertEqual(sig, qa.signal.SIGKILL)
        self.assertEqual(observed_pid, pid)
        with self.assertRaises(ChildProcessError):
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)

    def test_closed_pipes_do_not_remove_running_leader_deadline(self):
        code = "import os,time; os.close(1); os.close(2); time.sleep(20)"
        started = time.monotonic()
        with mock.patch.object(qa.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaises(qa.subprocess.TimeoutExpired):
                qa.bounded([sys.executable, "-c", code], timeout=.2)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(kill.call_count, 1)


if __name__ == "__main__":
    unittest.main()
