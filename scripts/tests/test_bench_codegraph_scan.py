"""Safety and provenance checks for the source-scan benchmark harness."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import bench_codegraph_scan as bench


class ScanBenchmarkTests(unittest.TestCase):
    def test_git_environment_overrides_fsmonitor_and_repository_routing(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/wrong/repo",
                                         "GIT_CONFIG_COUNT": "1",
                                         "GIT_CONFIG_KEY_0": "core.fsmonitor",
                                         "GIT_CONFIG_VALUE_0": "/untrusted/hook"}):
            env = bench.git_safe_env()
        self.assertNotIn("GIT_DIR", env)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.fsmonitor")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "false")
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_noisy_stdout_and_stderr_are_capped_before_process_exits(self):
        yes = shutil.which("yes")
        if yes is None:
            self.skipTest("coreutils yes is unavailable")
        stdout = bench.bounded_command([yes], 3, stdout_cap=4096)
        self.assertEqual(stdout["outcome"], "stdout_limit")
        self.assertLessEqual(len(stdout["stdout"]), 4097)
        self.assertLess(stdout["elapsed_ms"], 3000)
        stderr = bench.bounded_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 1000000)"],
            3, stderr_cap=4096,
        )
        self.assertEqual(stderr["outcome"], "stderr_limit")
        self.assertLessEqual(len(stderr["stderr"]), 4097)

    def test_timeout_reaps_a_child_process_group(self):
        result = bench.bounded_command(["/bin/sh", "-c", "sleep 3"], 0.1)
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(result["elapsed_ms"], 500)

    def test_expired_budget_never_starts_git_metadata(self):
        with mock.patch.object(bench, "bounded_command") as command:
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                bench.git_metadata(Path.home(), ["rev-parse", "HEAD"],
                                   time.perf_counter() - 1)
            command.assert_not_called()

    def test_git_metadata_uses_remaining_budget(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            fake_git = Path(temp) / "git"
            fake_git.write_text("#!/bin/sh\nsleep 2\n")
            fake_git.chmod(0o700)
            started = time.perf_counter()
            with mock.patch.dict(os.environ, {"PATH": f"{temp}:{os.environ['PATH']}"}):
                with self.assertRaisesRegex(RuntimeError, "Git metadata failed: timeout"):
                    bench.git_metadata(Path.home(), ["rev-parse", "HEAD"], started + 0.15)
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_symbol_hit_is_not_a_no_hit_sample(self):
        root = Path.home()
        for rows, omitted in (([{"name": "sentinel"}], 0), ([], 1)):
            answer = {"scope": str(root), "query": "symbol", "value": "sentinel",
                      "rows": rows, "omitted_rows": omitted}
            with mock.patch.object(bench, "bounded_command", return_value={
                "outcome": "ok", "stdout": json.dumps(answer).encode(), "stderr": b"",
                "exit_code": 0, "elapsed_ms": 1.0,
            }):
                result = bench.run_query(Path("/bin/true"), root, "sentinel", 1)
            self.assertEqual(result["outcome"], "invalid_reply")
            self.assertIn("symbol hit", result["error"])

    def test_dirty_status_is_recorded_and_fsmonitor_is_disabled(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            repo = Path(temp)
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            source = repo / "source.py"
            source.write_text("x = 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
            marker = repo / "hook-ran"
            hook = repo / "fsmonitor.sh"
            hook.write_text(f"#!/bin/sh\nprintf invoked > '{marker}'\n")
            hook.chmod(0o700)
            subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)],
                           check=True)
            command = ["git", "-C", str(repo), "ls-files", "--cached", "--others",
                       "--exclude-standard", "-z"]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
            if not marker.exists():
                self.skipTest("this Git version did not invoke the configured fsmonitor")
            marker.unlink()
            result = bench.bounded_command(command, 2)
            self.assertEqual(result["outcome"], "ok")
            self.assertFalse(marker.exists(), "benchmark child must not invoke fsmonitor")
            status = bench.status_evidence(repo, time.perf_counter() + 2)
            self.assertTrue(status["dirty"])
            self.assertGreater(status["entries"], 0)
            self.assertEqual(len(status["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
