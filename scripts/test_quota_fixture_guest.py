#!/usr/bin/env python3
"""Read-only adversarial checks for quota_fixture_read in a disposable guest.

The operator provisions a private four-bind ext4 fixture and runs this script
as guest root. It never sets a quota, writes a marker, or enables admission.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess


@contextmanager
def directory_fd(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def attempt(binary: Path, root: Path, project: int, limit: int, owner: int,
            descriptor: int | None = None,
            run_as_uid: int | None = None) -> subprocess.CompletedProcess[str]:
    command = [str(binary), "--fixture-only", str(root), str(project), str(limit), str(owner)]
    if descriptor is not None:
        command += ["--root-fd", str(descriptor)]
    return subprocess.run(command, text=True, capture_output=True,
                          pass_fds=() if descriptor is None else (descriptor,),
                          user=run_as_uid, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("root", type=Path)
    parser.add_argument("project_id", type=int)
    parser.add_argument("hard_limit_bytes", type=int)
    parser.add_argument("owner_uid", type=int)
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.root.is_absolute() or not args.binary.is_absolute():
        parser.error("run as root in a disposable guest with absolute paths")
    if not all((args.root / name).is_dir() for name in ("checkout", "home", "cache", "broker")):
        parser.error("four bind-source directories are required")

    def require_refusal(label: str, result: subprocess.CompletedProcess[str]) -> None:
        if result.returncode == 0 or "admissible_as_hard_quota" in result.stdout:
            raise RuntimeError(f"{label} unexpectedly passed: {result.stdout} {result.stderr}")
        print(json.dumps({"case": label, "refused": True, "reason": result.stderr.strip()}))

    positive = attempt(args.binary, args.root, args.project_id,
                       args.hard_limit_bytes, args.owner_uid)
    if positive.returncode:
        raise RuntimeError(f"exact descriptor fixture refused: {positive.stderr}")
    receipt = json.loads(positive.stdout)
    if (receipt.get("admissible_as_hard_quota") is not False
            or receipt.get("project_id") != args.project_id
            or receipt.get("hard_limit_bytes") != args.hard_limit_bytes):
        raise RuntimeError(f"unexpected positive fixture receipt: {receipt}")
    print(json.dumps({"case": "exact_four_binds", "snapshot": receipt}))

    with directory_fd(args.root) as pinned:
        same = attempt(args.binary, args.root, args.project_id,
                       args.hard_limit_bytes, args.owner_uid, pinned)
        if same.returncode or json.loads(same.stdout) != receipt:
            raise RuntimeError(f"pinned root descriptor refused: {same.stderr}")
    with directory_fd(args.root / "home") as swapped:
        require_refusal("substituted_home_fd", attempt(args.binary, args.root,
            args.project_id, args.hard_limit_bytes, args.owner_uid, swapped))
    require_refusal("wrong_directory", attempt(args.binary, args.root / "home",
        args.project_id, args.hard_limit_bytes, args.owner_uid))
    require_refusal("wrong_project", attempt(args.binary, args.root,
        args.project_id + 1, args.hard_limit_bytes, args.owner_uid))
    require_refusal("wrong_limit", attempt(args.binary, args.root,
        args.project_id, args.hard_limit_bytes + 512, args.owner_uid))
    require_refusal("unprivileged_owner", attempt(args.binary, args.root,
        args.project_id, args.hard_limit_bytes, args.owner_uid, run_as_uid=args.owner_uid))
    print(json.dumps({"fixture_read_only_checks_passed": 6,
                      "admissible_as_hard_quota": False}))


if __name__ == "__main__":
    main()
