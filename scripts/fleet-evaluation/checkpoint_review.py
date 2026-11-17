#!/usr/bin/env python3
"""Export blind checkpoint cards, freeze verdicts, and join citation reviews offline."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from blind_review import MAX_BYTES, identity, jsonl, private_directory, read_private, rows, write_private_bytes


VERDICTS = {"aligned", "uncertain", "drifted", "blocked"}
BASES = {"within_charter", "task_scope", "authority", "evidence", "contradiction",
         "budget", "deadline", "human_action", "unknown"}
MODELS = {"jev:jev-1.13.0", "subq:subq-snap-1.2"}
SOURCE_FIELDS = {"version", "id", "group_id", "source_sha256", "origin", "collection_context",
                 "split", "consented", "scrubbed", "snapshot", "model", "threshold", "latency_ms", "outcome"}
SNAPSHOT_FIELDS = {"charter", "assignments", "artifacts", "guard_observations",
                   "recent_handoff_evidence", "budget", "elapsed_deadline", "phase"}
DECISION_FIELDS = {"status", "choice", "basis", "citation_ids",
                   "alignment_confidence", "guard_confidence", "supported"}
EXPORT_FIELDS = {"version", "rows", "source_sha256", "guide_sha256", "blind_cards_sha256"}
FREEZE_FIELDS = {"version", "export_receipt_sha256", "verdicts_sha256", "label_source",
                 "citation_cards_sha256", "rows", "citation_rows"}
ARTIFACT_ID = re.compile(r"[a-zA-Z0-9_.-]{1,128}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def exact(value, fields, message):
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError(message)


def version_one(value):
    return type(value) is int and value == 1


def probability(value):
    return type(value) in (int, float) and 0 <= value <= 1


def artifact_id(value):
    return isinstance(value, str) and ARTIFACT_ID.fullmatch(value) and value != "none"


def supplied_ids(snapshot):
    exact(snapshot, SNAPSHOT_FIELDS, "use the exact scrubbed host checkpoint snapshot")
    for field in ("assignments", "artifacts", "guard_observations", "recent_handoff_evidence"):
        if not isinstance(snapshot[field], list):
            raise ValueError("invalid checkpoint snapshot field")
    if not isinstance(snapshot["charter"], dict) or not isinstance(snapshot["budget"], dict):
        raise ValueError("invalid checkpoint charter or budget")
    ids = set()
    if len(snapshot["artifacts"]) > 65:
        raise ValueError("checkpoint snapshot exceeds the Choice artifact limit")
    for artifact in snapshot["artifacts"]:
        exact(artifact, {"id", "evidence"}, "invalid supplied checkpoint artifact")
        if not artifact_id(artifact["id"]) or artifact["id"] in ids or not isinstance(artifact["evidence"], dict):
            raise ValueError("invalid or duplicate supplied checkpoint artifact")
        ids.add(artifact["id"])
    return ids


def validate_decision(outcome):
    if outcome == {"status": "unavailable"}:
        return
    exact(outcome, DECISION_FIELDS, "invalid recorded Choice projection")
    choice, basis = outcome["choice"], outcome["basis"]
    if (outcome["status"] != "decision" or not isinstance(choice, str) or choice not in VERDICTS
            or not isinstance(basis, str) or basis not in BASES):
        raise ValueError("invalid recorded checkpoint Choice or basis")
    refs = outcome["citation_ids"]
    if not isinstance(refs, list) or len(refs) > 1 or any(not artifact_id(ref) for ref in refs):
        raise ValueError("a recorded Choice has zero or one artifact citation")
    if type(outcome["supported"]) is not bool or (outcome["supported"] and not refs):
        raise ValueError("invalid recorded basis/evidence guard")
    confidence, guards = outcome["alignment_confidence"], outcome["guard_confidence"]
    if (not probability(confidence) or not isinstance(guards, list) or len(guards) != 3
            or not all(map(probability, guards)) or guards[0] not in (0.0, confidence)):
        raise ValueError("invalid recorded checkpoint confidence")


def source_rows(raw):
    values = rows(raw)
    ids, hashes, groups, configurations = set(), set(), {}, set()
    for row in values:
        exact(row, SOURCE_FIELDS, "invalid checkpoint source schema")
        origin, split, context = row["origin"], row["split"], row["collection_context"]
        if not version_one(row["version"]) or split not in ("development", "holdout"):
            raise ValueError("invalid checkpoint source version or split")
        if context not in ("natural_work", "controlled_task_pilot", "induced_risk"):
            raise ValueError("invalid checkpoint collection context")
        if split == "holdout" and context == "controlled_task_pilot":
            raise ValueError("controlled task pilots cannot supply checkpoint holdout rows")
        if row["scrubbed"] is not True:
            raise ValueError("checkpoint source requires a scrubbing attestation")
        if origin == "real" and row["consented"] is not True:
            raise ValueError("real checkpoint source requires consent")
        if origin == "synthetic" and (split != "development" or row["consented"] is not False):
            raise ValueError("synthetic checkpoints require development and no human consent claim")
        if origin not in ("real", "synthetic"):
            raise ValueError("invalid checkpoint source origin")
        row_id, group = identity(row["id"]), identity(row["group_id"])
        source_hash = row["source_sha256"]
        if not isinstance(source_hash, str) or not SHA256.fullmatch(source_hash) or source_hash in hashes or row_id in ids:
            raise ValueError("checkpoint IDs and source hashes must be distinct and canonical")
        ids.add(row_id)
        hashes.add(source_hash)
        if groups.setdefault(group, (split, context)) != (split, context):
            raise ValueError("one originating fleet cannot cross splits or collection contexts")
        model, threshold, latency = row["model"], row["threshold"], row["latency_ms"]
        if (not isinstance(model, str) or model not in MODELS or not probability(threshold)
                or type(latency) is not int or not 0 <= latency <= 120000):
            raise ValueError("pinned checkpoint model, threshold and observed bounded latency are required")
        configurations.add((origin, split, model, threshold))
        supplied_ids(row["snapshot"])
        validate_decision(row["outcome"])
    if len(configurations) != 1:
        raise ValueError("use one origin, split, model and threshold per checkpoint packet")
    return values


def blind_cards(values):
    return [{"version": 1, "id": row["id"], "snapshot": row["snapshot"]} for row in values]


def citation_cards(values):
    return [{"version": 1, "id": row["id"], "snapshot": row["snapshot"], "choice": row["outcome"]["choice"],
             "basis": row["outcome"]["basis"], "citation_ids": row["outcome"]["citation_ids"]}
            for row in values if row["outcome"].get("citation_ids")]


def write_directory(directory, files):
    if not directory.is_absolute() or directory.resolve() != directory:
        raise ValueError("use a new canonical absolute output directory")
    if any(len(raw) > MAX_BYTES for raw in files.values()):
        raise ValueError("output exceeds 2 MiB")
    directory.mkdir(mode=0o700)
    private_directory(directory)
    for name, raw in files.items():
        write_private_bytes(directory / name, raw)


def validate_guide(guide):
    if not guide.decode("utf-8").strip():
        raise ValueError("supply the exact label guide as nonempty UTF-8")


def export_packet(source, guide, directory):
    values = source_rows(source)
    validate_guide(guide)
    cards = jsonl(blind_cards(values))
    binding = {"guide_sha256": digest(guide), "blind_cards_sha256": digest(cards)}
    templates = [{"id": row["id"], **binding, "verdict": None, "reviewer": "", "rationale": ""}
                 for row in values]
    receipt = {"version": 1, "rows": len(values), "source_sha256": digest(source), **binding}
    write_directory(directory, {
        "sealed-source.jsonl": source,
        "label-guide.md": guide,
        "blind-cards.jsonl": cards,
        "verdict-template.jsonl": jsonl(templates),
        "export-receipt.jsonl": jsonl([receipt]),
    })
    return len(values)


def read_receipt(directory, name, fields):
    raw = read_private(directory / name)
    records = rows(raw)
    receipt = records[0]
    exact(receipt, fields, "invalid checkpoint review receipt")
    if len(records) != 1 or not version_one(receipt["version"]) or type(receipt["rows"]) is not int:
        raise ValueError("invalid checkpoint review receipt version or record count")
    return receipt, digest(raw)


def read_export(directory):
    private_directory(directory)
    receipt, receipt_hash = read_receipt(directory, "export-receipt.jsonl", EXPORT_FIELDS)
    source = read_private(directory / "sealed-source.jsonl")
    guide = read_private(directory / "label-guide.md")
    cards = read_private(directory / "blind-cards.jsonl")
    if (digest(source) != receipt["source_sha256"] or digest(guide) != receipt["guide_sha256"]
            or digest(cards) != receipt["blind_cards_sha256"]):
        raise ValueError("checkpoint source, guide or blind cards changed after export")
    values = source_rows(source)
    validate_guide(guide)
    if receipt["rows"] != len(values) or cards != jsonl(blind_cards(values)):
        raise ValueError("checkpoint blind cards do not match the sealed source")
    return values, receipt, receipt_hash


def labels_by_id(values, expected, binding, field, predicate):
    labels = {}
    for row in values:
        exact(row, {"id", "reviewer", "rationale", field, *binding}, "invalid checkpoint label schema")
        key = identity(row["id"])
        if key in labels:
            raise ValueError("duplicate checkpoint label")
        if any(row[name] != hash_value for name, hash_value in binding.items()):
            raise ValueError("checkpoint labels refer to a different guide or card file")
        if not predicate(row[field]) or any(not isinstance(row[name], str) or not row[name].strip()
                                            for name in ("reviewer", "rationale")):
            raise ValueError("each checkpoint label requires a judgment, reviewer and rationale")
        labels[key] = row[field]
    if labels.keys() != expected:
        raise ValueError("checkpoint labels must cover exactly the relevant cards")
    return labels


def verdict_labels(raw, values, receipt):
    binding = {name: receipt[name] for name in ("guide_sha256", "blind_cards_sha256")}
    return labels_by_id(rows(raw), {row["id"] for row in values}, binding,
                        "verdict", lambda value: isinstance(value, str) and value in VERDICTS)


def label_source(values):
    return "human" if values[0]["origin"] == "real" else "synthetic_reference"


def freeze_verdicts(packet, verdicts, attestation):
    values, export_receipt, export_hash = read_export(packet)
    source = label_source(values)
    if attestation != source:
        raise ValueError("attest independent human verdicts or synthetic references to match packet origin")
    verdict_labels(verdicts, values, export_receipt)
    citation_rows = citation_cards(values)
    cards = jsonl(citation_rows)
    binding = {"citation_cards_sha256": digest(cards)}
    templates = [{"id": row["id"], **binding, "supports_claim": None, "reviewer": "", "rationale": ""}
                 for row in citation_rows]
    receipt = {"version": 1, "export_receipt_sha256": export_hash, "verdicts_sha256": digest(verdicts),
               "label_source": source, **binding, "rows": len(values), "citation_rows": len(citation_rows)}
    # Persist the verdicts and their binding before disclosing any model judgment.
    write_directory(packet / "frozen", {
        "frozen-verdicts.jsonl": verdicts,
        "freeze-receipt.jsonl": jsonl([receipt]),
        "citation-cards.jsonl": cards,
        "citation-template.jsonl": jsonl(templates),
    })
    return len(values)


def read_frozen(directory, values, export_receipt, export_hash):
    private_directory(directory)
    receipt, receipt_hash = read_receipt(directory, "freeze-receipt.jsonl", FREEZE_FIELDS)
    verdicts = read_private(directory / "frozen-verdicts.jsonl")
    cards = read_private(directory / "citation-cards.jsonl")
    citation_rows = citation_cards(values)
    if (export_hash != receipt["export_receipt_sha256"] or digest(verdicts) != receipt["verdicts_sha256"]
            or digest(cards) != receipt["citation_cards_sha256"] or cards != jsonl(citation_rows)
            or receipt["label_source"] != label_source(values)
            or receipt["rows"] != len(values) or type(receipt["citation_rows"]) is not int
            or receipt["citation_rows"] != len(citation_rows)):
        raise ValueError("frozen checkpoint verdicts or citation cards no longer match their receipts")
    return verdict_labels(verdicts, values, export_receipt), citation_rows, receipt, receipt_hash


def evaluation_row(source, verdict, citation_support):
    row = {name: value for name, value in source.items() if name not in {"outcome", "snapshot"}}
    outcome = source["outcome"]
    if outcome["status"] == "decision":
        refs = outcome["citation_ids"]
        valid = all(ref in supplied_ids(source["snapshot"]) for ref in refs)
        supported = citation_support[row["id"]] if refs else True
        if supported and not valid:
            raise ValueError("a missing artifact cannot support the checkpoint claim")
        outcome = {name: value for name, value in outcome.items() if name not in {"basis", "citation_ids"}}
        outcome.update(citations_valid=valid, citations_supported=supported)
    return {**row, "label_source": "human" if source["origin"] == "real" else "synthetic_reference",
            "human": verdict, "outcome": outcome}


def join_packet(packet, citation_labels, directory, independent_citation_review):
    values, export_receipt, export_hash = read_export(packet)
    verdicts, citation_rows, freeze_receipt, freeze_hash = read_frozen(packet / "frozen", values, export_receipt, export_hash)
    if independent_citation_review != (bool(citation_rows) and label_source(values) == "human"):
        raise ValueError("attest independent citation support review for real cited decisions only")
    support = {}
    if citation_rows:
        if citation_labels is None:
            raise ValueError("citation support labels are required after the verdict freeze")
        support = labels_by_id(rows(citation_labels), {row["id"] for row in citation_rows},
                               {"citation_cards_sha256": freeze_receipt["citation_cards_sha256"]},
                               "supports_claim", lambda value: type(value) is bool)
    elif citation_labels is not None:
        raise ValueError("no citation support labels are needed for this packet")
    result = [evaluation_row(row, verdicts[row["id"]], support) for row in values]
    receipt = {"version": 1, "freeze_receipt_sha256": freeze_hash, "rows": len(result),
               "citation_labels_sha256": None if citation_labels is None else digest(citation_labels),
               "independent_citation_review": independent_citation_review}
    evaluation = jsonl(result)
    write_directory(directory, {
        "labeled-checkpoints.jsonl": evaluation,
        "join-receipt.jsonl": jsonl([{**receipt, "evaluation_sha256": digest(evaluation)}]),
    })
    return len(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("source", type=Path)
    export.add_argument("guide", type=Path)
    export.add_argument("new_packet", type=Path)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("packet", type=Path)
    freeze.add_argument("verdicts", type=Path)
    label_kind = freeze.add_mutually_exclusive_group(required=True)
    label_kind.add_argument("--independent-human-labels", dest="attestation", action="store_const", const="human")
    label_kind.add_argument("--synthetic-reference", dest="attestation", action="store_const", const="synthetic_reference")
    join = commands.add_parser("join")
    join.add_argument("packet", type=Path)
    join.add_argument("new_result", type=Path)
    join.add_argument("--citation-labels", type=Path)
    join.add_argument("--independent-citation-review", action="store_true")
    args = parser.parse_args()
    if args.command == "export":
        count = export_packet(read_private(args.source), read_private(args.guide), args.new_packet)
    elif args.command == "freeze":
        count = freeze_verdicts(args.packet, read_private(args.verdicts), args.attestation)
    else:
        labels = read_private(args.citation_labels) if args.citation_labels is not None else None
        count = join_packet(args.packet, labels, args.new_result, args.independent_citation_review)
    print(json.dumps({"rows": count, "model_calls": 0}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise SystemExit("Checkpoint review refused: " + str(error))
