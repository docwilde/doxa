#!/usr/bin/env python3
"""Actual local child/PIDFD races; substituted metadata never admits isolation."""
import json
import os
from pathlib import Path

from test_root_broker import NativeGatewayExitRaceTests, b


def main():
    root=Path(__file__).resolve().parent
    cases=[NativeGatewayExitRaceTests().exercise_stop_race(race)
           for race in ("exit_before_capture","exit_before_sigterm")]
    print(json.dumps({"contract":"doxa-local-gateway-exit-race-proof-v1","broker_sha256":b.sha_file(root/"root_broker.py"),
                      "scope":"actual benign local child/PIDFD and Unix socket inodes; ROOT metadata/Engine are fixtures",
                      "fixture_uid":os.geteuid(),"root_metadata_substituted":True,"live_admission_proved":False,
                      "guest_mutations":False,"cases":cases,"status":"passed"},indent=2,sort_keys=True))


if __name__=="__main__":main()
