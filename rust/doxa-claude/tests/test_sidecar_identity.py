"""Start identity checks must run before the SDK engine is constructed."""

import importlib.util
import pathlib
import unittest


SIDECAR = pathlib.Path(__file__).resolve().parents[1] / "claude_sidecar.py"
spec = importlib.util.spec_from_file_location("claude_sidecar", SIDECAR)
sidecar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar)


class IdentityTests(unittest.TestCase):
    def test_fresh_id_and_resume(self):
        self.assertEqual(sidecar.validate_identity("abc-123", None), ("abc-123", None))
        self.assertEqual(sidecar.validate_identity(None, "abc-123"), ("abc-123", "abc-123"))
        self.assertEqual(sidecar.validate_identity("abc-123", "abc-123"),
                         ("abc-123", "abc-123"))

    def test_rejects_unsafe_and_mismatched_ids(self):
        for session_id, resume in [
            ("../escape", None), (None, "../escape"),
            ("abc", "different"), ("", None), (None, ""),
            (12, None), (None, 12),
        ]:
            with self.subTest(session_id=session_id, resume=resume):
                with self.assertRaises(ValueError):
                    sidecar.validate_identity(session_id, resume)


if __name__ == "__main__":
    unittest.main()
