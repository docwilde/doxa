#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Check fresh offline emulator recovery UI, without printing captured content."""

import sys
import xml.etree.ElementTree as ET

LIMIT = 131_072


def check(raw, mode):
    if mode not in ("readable", "corrupt") or len(raw) > LIMIT:
        return False
    try:
        root = ET.fromstring(raw)
        nodes = list(root.iter("node"))
        texts = {node.get("text", "") for node in nodes}
        if "Write outcome needs review" not in texts:
            return False
        acknowledgments = [node for node in nodes if node.get("text") == "Acknowledge after review"]
        if mode == "corrupt":
            return any("A prior write marker is unreadable" in text for text in texts) and not acknowledgments
        if not any("A prompt to qa-host~qa-session (incarnation qa-incarnation)" in text for text in texts) \
                or not any("The request body was not saved" in text for text in texts) \
                or len(acknowledgments) != 1:
            return False
        parents = {child: parent for parent in root.iter() for child in parent}
        action = acknowledgments[0]
        # Compose's TextView label is enabled even inside a disabled button.
        # Sample the nearest actionable control, never the decorative label.
        while action is not None:
            if action.get("clickable") == "true" or action.get("class") == "android.widget.Button":
                return action.get("enabled") == "false"
            action = parents.get(action)
        return False
    except (ET.ParseError, ValueError, TypeError):
        return False


if __name__ == "__main__":
    sys.exit(0 if len(sys.argv) == 2 and check(sys.stdin.buffer.read(LIMIT + 1), sys.argv[1]) else 1)
