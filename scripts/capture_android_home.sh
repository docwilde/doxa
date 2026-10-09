#!/usr/bin/env bash
# Capture the unconfigured Android Remote home screen from a running emulator.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
adb="${ANDROID_HOME:?Set ANDROID_HOME to the Android SDK}/platform-tools/adb"
serial="${ANDROID_SERIAL:?Set ANDROID_SERIAL to the dedicated emulator serial}"
apk="${1:-$repo_root/android-client/app/build/outputs/apk/debug/app-debug.apk}"
output="${2:-$repo_root/assets/shots/android-remote-beta30-home-offline.png}"
package="ai.ampiric.doxa.remote"

test -x "$adb"
test -f "$apk"
test "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 || {
    echo "Refusing to capture from a non-emulator device" >&2
    exit 1
}
test "$("$adb" -s "$serial" shell getprop sys.boot_completed | tr -d '\r')" = 1

"$adb" -s "$serial" shell cmd connectivity airplane-mode enable
"$adb" -s "$serial" shell svc wifi disable
"$adb" -s "$serial" shell svc data disable
test "$("$adb" -s "$serial" shell cmd connectivity airplane-mode | tr -d '\r')" = enabled
"$adb" -s "$serial" shell cmd uimode night no >/dev/null
"$adb" -s "$serial" shell settings put system font_scale 1.0
"$adb" -s "$serial" install -r "$apk"
"$adb" -s "$serial" shell pm clear "$package"
"$adb" -s "$serial" shell am start -n "$package/.MainActivity" >/dev/null

# Compose accessibility may become ready a little after Activity launch.
for attempt in {1..20}; do
    "$adb" -s "$serial" shell uiautomator dump /sdcard/doxa-home-window.xml >/dev/null 2>&1 || true
    if "$adb" -s "$serial" exec-out cat /sdcard/doxa-home-window.xml 2>/dev/null |
        python3 -c 'import sys, xml.etree.ElementTree as ET
try:
    nodes = list(ET.parse(sys.stdin).getroot().iter("node"))
except ET.ParseError:
    sys.exit(1)
texts = {node.get("text", "") for node in nodes}
inputs = [node for node in nodes if node.get("class") == "android.widget.EditText"]
assert {"DOXA Remote", "Private Tailscale hub URL", "Choose shared key", "Connect"} <= texts
assert len(inputs) == 1 and inputs[0].get("text") == ""
assert "Write outcome needs review" not in texts
heading = next(node for node in nodes if node.get("text") == "DOXA Remote")
connect = next(node for node in nodes if node.get("text") == "Connect")
assert int(heading.get("bounds").split(",")[1].split("]")[0]) >= 100
assert int(connect.get("bounds").rsplit(",", 1)[1].split("]")[0]) <= 2300
'; then
        mkdir -p "$(dirname "$output")"
        partial="$output.incomplete.$$"
        trap 'rm -f "$partial"' EXIT
        "$adb" -s "$serial" exec-out screencap -p > "$partial"
        python3 -c 'import struct, sys
with open(sys.argv[1], "rb") as image:
    header = image.read(24)
assert header[:8] == b"\x89PNG\r\n\x1a\n"
assert struct.unpack(">II", header[16:24]) == (1080, 2400)
' "$partial"
        mv "$partial" "$output"
        echo "$output (1080 x 2400; offline, clean app data)"
        exit 0
    fi
    sleep 1
done

echo "The clean DOXA Remote home screen did not appear" >&2
exit 1
