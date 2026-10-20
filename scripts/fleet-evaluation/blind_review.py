#!/usr/bin/env python3
"""Prepare private blind human-review cards; never call a model or invent labels."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

MAX_BYTES = 2 * 1024 * 1024
FIELDS = {"within_assignment", "asks_for_authority_change", "contains_instructions_for_recipient", "likely_secret", "needs_human_review"}
SOURCE_FIELDS = {"version", "id", "group_id", "origin", "split", "consented", "scrubbed", "charter", "assignment", "message", "model", "verdict", "latency_ms", "source_sha256"}


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        meta = os.fstat(file.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) & 0o077 or meta.st_size > MAX_BYTES:
            raise ValueError("input must be a bounded private owner-owned regular file")
        raw = file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("input exceeds 2 MiB")
    return raw


def rows(raw):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    values = [json.loads(line, object_pairs_hook=unique_object) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not 1 <= len(values) <= 10000:
        raise ValueError("need 1–10000 rows")
    return values


def identity(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("identities must be canonical opaque UUIDs")
    return value


def validate_source(values):
    seen, groups, models = set(), {}, set()
    for row in values:
        if set(row) != SOURCE_FIELDS or row["version"] != 1 or row["origin"] != "real" or row["consented"] is not True or row["scrubbed"] is not True:
            raise ValueError("source needs exact schema and operator-attested real consented scrubbed provenance")
        key, group = identity(row["id"]), identity(row["group_id"])
        if key in seen or row["split"] not in {"development", "holdout"}:
            raise ValueError("duplicate message or unsupported split")
        seen.add(key)
        if group in groups and groups[group] != row["split"]:
            raise ValueError("a fleet cannot cross development and holdout")
        groups[group] = row["split"]
        if not isinstance(row["model"], str) or ":" not in row["model"] or len(row["model"]) > 160:
            raise ValueError("exact provider:model is required")
        models.add(row["model"])
        verdict = row["verdict"]
        if not isinstance(verdict, dict) or set(verdict) != FIELDS or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in verdict.values()):
            raise ValueError("invalid recorded production verdict")
        if isinstance(row["latency_ms"], bool) or not isinstance(row["latency_ms"], int) or row["latency_ms"] < 0:
            raise ValueError("observed latency is required; do not invent missing usage or timing")
        digest = row["source_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("bind each source record by SHA256")
    if len(models) != 1:
        raise ValueError("use one fixed judge model per review")
    return values


def cards(values):
    return [{k: row[k] for k in ("version", "id", "group_id", "split", "charter", "assignment", "message", "source_sha256")} for row in values]


def join(values, labels, independent_human_labels):
    if not independent_human_labels:
        raise ValueError("an explicit independent human-label attestation is required")
    by_id = {}
    for label in labels:
        if set(label) != {"id", "risky", "reviewer", "rationale"} or not isinstance(label["risky"], bool) or not isinstance(label["reviewer"], str) or not label["reviewer"].strip() or not isinstance(label["rationale"], str):
            raise ValueError("every human label needs risky, reviewer and rationale; blank templates are not labels")
        key = identity(label["id"])
        if key in by_id:
            raise ValueError("duplicate human label")
        by_id[key] = label
    if set(by_id) != {row["id"] for row in values}:
        raise ValueError("human labels must cover exactly the blind export")
    return [{"version": 1, "id": row["id"], "group_id": row["group_id"], "origin": "real", "split": row["split"],
             "consented": True, "scrubbed": True, "label_source": "human", "risky": by_id[row["id"]]["risky"],
             "model": row["model"], "verdict": row["verdict"], "latency_ms": row["latency_ms"]} for row in values]


def write_new(path, values):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        for value in values:
            file.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def private_directory(path):
    meta = path.lstat()
    if not path.is_absolute() or not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) & 0o077 or path.resolve() != path:
        raise ValueError("output directory must be canonical and owner-private")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("source", type=Path)
    export.add_argument("new_directory", type=Path)
    assemble = commands.add_parser("join")
    assemble.add_argument("directory", type=Path)
    assemble.add_argument("human_labels", type=Path)
    assemble.add_argument("--independent-human-labels", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        raw = read_private(args.source)
        values = validate_source(rows(raw))
        directory = args.new_directory
        if not directory.is_absolute():
            raise ValueError("use an absolute private output directory")
        directory.mkdir(mode=0o700)
        private_directory(directory)
        write_new(directory / "sealed-source.jsonl", values)
        write_new(directory / "blind-cards.jsonl", cards(values))
        write_new(directory / "labels-template.jsonl", [{"id": row["id"], "risky": None, "reviewer": "", "rationale": ""} for row in values])
        write_new(directory / "export-receipt.jsonl", [{"source_sha256": hashlib.sha256(raw).hexdigest(), "rows": len(values), "automatic_labels": False, "provenance_is_operator_attestation": True}])
    else:
        private_directory(args.directory)
        values = validate_source(rows(read_private(args.directory / "sealed-source.jsonl")))
        labels_raw = read_private(args.human_labels)
        result = join(values, rows(labels_raw), args.independent_human_labels)
        write_new(args.directory / "human-labeled-evaluation.jsonl", result)
        write_new(args.directory / "join-receipt.jsonl", [{"labels_sha256": hashlib.sha256(labels_raw).hexdigest(), "rows": len(result), "independent_human_attestation": True, "gate_evaluated": False}])
    print(json.dumps({"rows": len(values), "model_calls": 0, "automatic_labels": False, "enforcement_changed": False}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise SystemExit("Blind review refused: " + str(error))
