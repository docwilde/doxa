# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("recovery_ui",
    Path(__file__).resolve().parents[1] / "check_android_recovery_ui.py")
ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui)


def frame(enabled="false", label_enabled="true", action=True):
    return ('<hierarchy><node text="Write outcome needs review"/>'
            '<node text="A prompt to qa-host~qa-session (incarnation qa-incarnation)"/>'
            '<node text="The request body was not saved"/>'
            f'<node clickable="{str(action).lower()}" enabled="{enabled}">'
            f'<node class="android.widget.TextView" text="Acknowledge after review" enabled="{label_enabled}"/>'
            '</node></hierarchy>')


class RecoveryUITests(unittest.TestCase):
    def test_enabled_label_inside_disabled_compose_action_passes(self):
        self.assertTrue(ui.check(frame(), "readable"))

    def test_disabled_label_cannot_disguise_enabled_action(self):
        self.assertFalse(ui.check(frame(enabled="true", label_enabled="false"), "readable"))

    def test_missing_action_or_duplicate_acknowledgment_fails(self):
        self.assertFalse(ui.check(frame(action=False), "readable"))
        self.assertFalse(ui.check(frame().replace('</hierarchy>',
            '<node text="Acknowledge after review"/></hierarchy>'), "readable"))

    def test_corrupt_marker_requires_warning_and_no_bypass(self):
        raw = '<hierarchy><node text="Write outcome needs review"/><node text="A prior write marker is unreadable"/></hierarchy>'
        self.assertTrue(ui.check(raw, "corrupt"))
        self.assertFalse(ui.check(raw.replace('</hierarchy>',
            '<node text="Acknowledge after review"/></hierarchy>'), "corrupt"))

    def test_malformed_oversized_or_wrong_screen_is_refused(self):
        for raw, mode in (("<bad", "readable"), (frame(), "unknown"),
                          ("x" * (ui.LIMIT + 1), "readable"),
                          ('<hierarchy><node text="DOXA Remote"/></hierarchy>', "corrupt")):
            self.assertFalse(ui.check(raw, mode))


if __name__ == "__main__":
    unittest.main()
