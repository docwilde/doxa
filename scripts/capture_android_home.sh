#!/usr/bin/env bash
# Capture Android Remote only from a fresh, private emulator data directory.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sdk="${ANDROID_HOME:?Set ANDROID_HOME to an Android SDK with the emulator and Android 36 system image}"
scratch="${TMPDIR:?Set TMPDIR to a writable directory on real disk}"
adb="$sdk/platform-tools/adb"
emulator="${ANDROID_EMULATOR_BIN:-$sdk/emulator/emulator}"
avdmanager="${ANDROID_AVDMANAGER_BIN:-$sdk/cmdline-tools/latest/bin/avdmanager}"
apk="${1:-$repo_root/android-client/app/build/outputs/apk/debug/app-debug.apk}"
output="${2:-$repo_root/assets/shots/android-remote-beta30-home-offline.png}"
recovery_output="${DOXA_CAPTURE_RECOVERY_OUTPUT:-}"
port="${DOXA_CAPTURE_PORT:-5580}"
package="ai.ampiric.doxa.remote"

if [[ -n "$recovery_output" ]]; then
    [[ "$recovery_output" = /* ]] || { echo "Recovery output must be absolute" >&2; exit 1; }
    [[ "$(realpath -m "$recovery_output")" != "$(realpath -m "$output")" ]] || {
        echo "Home and recovery outputs must differ" >&2; exit 1;
    }
    for target in "$recovery_output" "$recovery_output.receipt.json"; do
        [[ ! -e "$target" && ! -L "$target" ]] || { echo "Recovery evidence already exists; choose a fresh output" >&2; exit 1; }
    done
fi

case "$(realpath -m "$scratch")" in /tmp|/tmp/*) echo "TMPDIR must be on real disk" >&2; exit 1;; esac
[[ "$port" =~ ^[0-9]+$ ]] && (( port >= 5554 && port <= 5584 && port % 2 == 0 )) || {
    echo "DOXA_CAPTURE_PORT must be an even emulator port from 5554 to 5584" >&2
    exit 1
}
for binary in "$adb" "$emulator" "$avdmanager"; do test -x "$binary"; done
test -f "$apk"
mkdir -p "$scratch"
serial="emulator-$port"
if "$adb" devices | awk 'NR > 1 { print $1 }' | grep -Fxq "$serial"; then
    echo "$serial is already in use; refusing to touch an existing emulator" >&2
    exit 1
fi

capture_dir="$(mktemp -d "$scratch/doxa-android-home.XXXXXX")"
avd_name="doxa-home-${capture_dir##*.}"
export ANDROID_AVD_HOME="$capture_dir/avd"
mkdir -p "$ANDROID_AVD_HOME"
emulator_pid=""
dest_stage=""
cleanup() {
    status=$?
    trap - EXIT
    if [[ -n "$emulator_pid" ]] && kill -0 "$emulator_pid" 2>/dev/null; then
        "$adb" -s "$serial" emu kill >/dev/null 2>&1 || true
        wait "$emulator_pid" >/dev/null 2>&1 || true
    fi
    if [[ -n "$dest_stage" && -f "$dest_stage" ]]; then unlink "$dest_stage"; fi
    python3 - "$capture_dir" "$scratch" <<'PY'
from pathlib import Path
import shutil
import sys
capture = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()
if capture.parent != scratch or not capture.name.startswith("doxa-android-home."):
    raise SystemExit("Refusing to remove an unexpected capture directory")
shutil.rmtree(capture)
PY
    exit "$status"
}
trap cleanup EXIT

"$avdmanager" create avd -n "$avd_name" \
    -k 'system-images;android-36;default;x86_64' \
    -p "$ANDROID_AVD_HOME/$avd_name.avd" --device pixel_6 >/dev/null
"$emulator" -avd "$avd_name" -port "$port" -no-window -no-snapshot \
    -no-audio -no-boot-anim -gpu swiftshader_indirect >"$capture_dir/emulator.log" 2>&1 &
emulator_pid=$!

booted=false
for attempt in {1..120}; do
    if ! kill -0 "$emulator_pid" 2>/dev/null; then
        echo "Dedicated emulator exited during boot" >&2
        tail -30 "$capture_dir/emulator.log" >&2
        exit 1
    fi
    if [[ "$(timeout 5 "$adb" -s "$serial" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]]; then
        booted=true
        break
    fi
    sleep 1
done
test "$booted" = true || { echo "Dedicated emulator did not boot" >&2; exit 1; }
test "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1
test "$("$adb" -s "$serial" shell getprop ro.boot.qemu.avd_name | tr -d '\r')" = "$avd_name" || {
    echo "Emulator AVD identity did not match the fresh data directory" >&2
    exit 1
}
test -z "$("$adb" -s "$serial" shell pm path "$package" | tr -d '\r')" || {
    echo "DOXA Remote was already installed in this emulator" >&2
    exit 1
}
test -z "$("$adb" -s "$serial" shell pm list packages -3 | tr -d '\r')" || {
    echo "Fresh emulator contains a third-party package" >&2
    exit 1
}

"$adb" -s "$serial" shell cmd connectivity airplane-mode enable
"$adb" -s "$serial" shell svc wifi disable
"$adb" -s "$serial" shell svc data disable
test "$("$adb" -s "$serial" shell cmd connectivity airplane-mode | tr -d '\r')" = enabled
"$adb" -s "$serial" shell cmd uimode night no >/dev/null
"$adb" -s "$serial" shell settings put system font_scale 1.0
"$adb" -s "$serial" install "$apk"
"$adb" -s "$serial" shell am start -n "$package/.MainActivity" >/dev/null

# Only the exact connection screen may reach the screenshot stage.
ready=false
for attempt in {1..20}; do
    "$adb" -s "$serial" shell uiautomator dump /sdcard/doxa-home-window.xml >/dev/null 2>&1 || true
    if "$adb" -s "$serial" exec-out cat /sdcard/doxa-home-window.xml 2>/dev/null |
        python3 -c 'import sys, xml.etree.ElementTree as ET
try:
    nodes = list(ET.parse(sys.stdin).getroot().iter("node"))
except ET.ParseError:
    sys.exit(1)
texts = {node.get("text", "") for node in nodes if node.get("text")}
expected = {"DOXA Remote", "Connect through your user-owned Tailscale device",
            "Private Tailscale hub URL", "Choose shared key", "Connect",
            "Key stays in memory for this app run. Tailscale signs in the device."}
assert texts == expected
inputs = [node for node in nodes if node.get("class") == "android.widget.EditText"]
assert len(inputs) == 1 and inputs[0].get("text") == ""
heading = next(node for node in nodes if node.get("text") == "DOXA Remote")
connect = next(node for node in nodes if node.get("text") == "Connect")
assert int(heading.get("bounds").split(",")[1].split("]")[0]) >= 100
assert int(connect.get("bounds").rsplit(",", 1)[1].split("]")[0]) <= 2300
' >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
test "$ready" = true || { echo "Expected clean connection screen did not appear" >&2; exit 1; }

screen_stage="$capture_dir/home.png"
"$adb" -s "$serial" exec-out screencap -p > "$screen_stage"
python3 - "$screen_stage" <<'PY'
from PIL import Image
import sys
path = sys.argv[1]
with Image.open(path) as image:
    assert image.format == "PNG" and image.size == (1080, 2400)
    image.verify()
with Image.open(path) as image:
    image.load()  # Decode all pixels; a valid header is insufficient.
    assert image.size == (1080, 2400)
    assert set(image.info) <= {"srgb"}
PY
mkdir -p "$(dirname "$output")"
dest_stage="$(mktemp "${output}.incomplete.XXXXXX")"
cp "$screen_stage" "$dest_stage"
cmp -s "$screen_stage" "$dest_stage"
mv "$dest_stage" "$output"
dest_stage=""
echo "$output (1080 x 2400; private fresh AVD, offline, empty URL)"

# Optional release QA: only this fresh offline AVD receives synthetic markers.
# This exercises persisted startup UI, never a real submission or FCM delivery.
if [[ -n "$recovery_output" ]]; then
    python3 - "$capture_dir" <<'PY'
import json, sys, xml.etree.ElementTree as ET
from pathlib import Path
root = Path(sys.argv[1])
marker = dict(v=1, origin="https://owner.tailnet.ts.net/", target="qa-host~qa-session",
              incarnation="qa-incarnation", operation="prompt",
              request_id="0" * 32 + "-11111111-1111-4111-8111-111111111111", created_at_ms=1000)
for name, value in (("readable", json.dumps(marker)), ("corrupt", "{invalid")):
    tree = ET.Element("map")
    ET.SubElement(tree, "string", name="pending_write_marker_v1").text = value
    ET.ElementTree(tree).write(root / (name + ".xml"), encoding="utf-8", xml_declaration=True)
PY
    inject_marker() {
        "$adb" -s "$serial" shell am force-stop "$package"
        "$adb" -s "$serial" shell run-as "$package" mkdir -p shared_prefs
        "$adb" -s "$serial" shell run-as "$package" rm -f shared_prefs/doxa-remote.xml.bak
        "$adb" -s "$serial" shell "run-as $package sh -c 'cat > shared_prefs/doxa-remote.xml'" < "$capture_dir/$1.xml"
        "$adb" -s "$serial" exec-out run-as "$package" cat shared_prefs/doxa-remote.xml | cmp -s - "$capture_dir/$1.xml"
    }
    check_recovery() {
        local mode="$1" recovered=false
        "$adb" -s "$serial" shell am start -n "$package/.MainActivity" >/dev/null
        for attempt in {1..20}; do
            "$adb" -s "$serial" shell uiautomator dump /sdcard/doxa-home-window.xml >/dev/null 2>&1 || true
            if "$adb" -s "$serial" exec-out cat /sdcard/doxa-home-window.xml 2>/dev/null |
                python3 "$repo_root/scripts/check_android_recovery_ui.py" "$mode" >/dev/null 2>&1; then
                recovered=true
                break
            fi
            sleep 1
        done
        if [[ "$recovered" != true ]]; then
            mkdir -p "$(dirname "$recovery_output")"
            "$adb" -s "$serial" exec-out cat /sdcard/doxa-home-window.xml > "$recovery_output.failed.xml" || true
            "$adb" -s "$serial" exec-out screencap -p > "$recovery_output.failed.png" || true
            echo "Offline $mode marker recovery check failed; synthetic UI evidence retained" >&2
            exit 1
        fi
    }
    inject_marker readable
    check_recovery readable
    "$adb" -s "$serial" shell am force-stop "$package"
    check_recovery readable
    "$adb" -s "$serial" exec-out screencap -p > "$capture_dir/recovery.png"
    python3 - "$capture_dir/recovery.png" <<'PY'
from PIL import Image
import sys
with Image.open(sys.argv[1]) as image:
    assert image.format == "PNG" and image.size == (1080, 2400)
    image.verify()
with Image.open(sys.argv[1]) as image:
    image.load()
PY
    inject_marker corrupt
    check_recovery corrupt
    mkdir -p "$(dirname "$recovery_output")"
    dest_stage="$(mktemp "${recovery_output}.incomplete.XXXXXX")"
    cp "$capture_dir/recovery.png" "$dest_stage"
    cmp -s "$capture_dir/recovery.png" "$dest_stage"
    mv "$dest_stage" "$recovery_output"
    dest_stage=""
    python3 - "$apk" "$recovery_output.receipt.json" <<'PY'
import datetime, hashlib, json, os, sys
receipt = dict(schema="doxa-android-offline-recovery-v1", status="passed",
               observed_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               apk_sha256=hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(),
               fresh_private_emulator=True, offline=True, synthetic_marker=True,
               readable_marker_blocked=True, relaunch_still_blocked=True,
               corrupt_marker_blocked=True, submitted_requests=0,
               physical_device_delivery="unknown", fcm_delivery="unknown")
with open(sys.argv[2], "x") as output:
    os.chmod(sys.argv[2], 0o600)
    json.dump(receipt, output, sort_keys=True)
PY
    echo "$recovery_output (offline synthetic marker: readable, relaunch, corrupt checks passed)"
fi
