"""Durable admission for the frozen evaluation pilot. No transport or credentials."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

ROOT = Path(__file__).resolve().parent
MICROS = 1_000_000
MAX_MEASUREMENT = 2**32 - 1
JEV_ARM = "jev_per_call"
FIXED_ARMS = {
    "fixed_flash_none": ("deepseek-flash", "none"),
    "fixed_pro_high": ("deepseek-v4-pro", "high"),
}


class LedgerRefused(ValueError):
    """Admission or settlement failed. A caller must never dispatch after refusal."""


def require(condition, message):
    if not condition:
        raise LedgerRefused(message)


def check_digest(digest):
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "expected a lower-case SHA-256 digest")


def bounded_int(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def micros(value):
    scaled = Decimal(value) * MICROS
    require(scaled.is_finite() and scaled >= 0 and scaled == scaled.to_integral_value(),
            "pilot dollar amount must be expressible in integer USD micros")
    return int(scaled)


def token_cost(input_tokens, output_tokens, input_rate, output_rate):
    # Match the integer rounding of doxa_router::token_cost.
    return (input_tokens * input_rate + output_tokens * output_rate + MICROS - 1) // MICROS


def policy():
    raw = (ROOT / "pilot.json").read_bytes()
    plan = json.loads(raw)
    allocation_caps = {key: micros(amount) for key, amount in plan["allocations_usd"].items()}
    require(plan["schema"] == "doxa-evaluation-pilot-v1"
            and allocation_caps == {"repair": 15 * MICROS, "decisions": 2 * MICROS,
                                    "fleet_development": 5 * MICROS, "undispatched": 3 * MICROS}
            and sum(allocation_caps.values()) == micros(plan["authorized_usd"]),
            "pilot allocations changed")
    tasks = {"easy-config", "easy-expiry", "intermediate-graph", "intermediate-delegation", "difficult-budget"}
    require(len(plan["tasks"]) == 5 and set(plan["tasks"]) == tasks
            and len(plan["arms"]) == 3 and set(plan["arms"]) == {*FIXED_ARMS, JEV_ARM}
            and plan["trials_per_task_per_arm"] == 1 and plan["max_calls_per_trial"] == 6
            and plan["max_input_tokens"] == 20_000 and plan["max_output_tokens"] == 32_768
            and plan["max_fleets"] == 2 and micros(plan["usd_per_fleet"]) == 2_500_000,
            "pilot trial or fleet scope changed")
    binding = (hashlib.sha256(raw).hexdigest(),
               hashlib.sha256((ROOT / "corpus.json").read_bytes()).hexdigest())
    return plan, allocation_caps, binding


def private_path(path, *, existing):
    path = Path(path).absolute()
    parent = path.parent.stat()
    require(path.parent.resolve(strict=True) == path.parent and stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == os.getuid() and not parent.st_mode & 0o077,
            "pilot ledger requires a canonical owner-private directory")
    if existing:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                "pilot ledger requires an owner-only regular file")
    return path


@dataclass(frozen=True)
class Reservation:
    id: str
    held_usd_micros: int


class PilotLedger:
    def __init__(self, path):
        self._path = path
        self._plan, self._caps, self._binding = policy()
        self._run_id = None

    @classmethod
    def create(cls, path):
        """Initialize once at the authorized pilot path. Never creates on resume."""
        path = private_path(path, existing=False)
        ledger = cls(path)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        os.close(descriptor)
        run_id = uuid.uuid4().hex
        with ledger._transaction(write=True, initialize=True) as db:
            db.execute("""CREATE TABLE pilot (
                id INTEGER PRIMARY KEY CHECK (id = 1), run_id TEXT NOT NULL,
                pilot_sha256 TEXT NOT NULL, corpus_sha256 TEXT NOT NULL
            )""")
            db.execute("INSERT INTO pilot (id, run_id, pilot_sha256, corpus_sha256) VALUES (1, ?, ?, ?)",
                       (run_id, *ledger._binding))
            db.execute("""CREATE TABLE reservations (
                id TEXT PRIMARY KEY, allocation TEXT NOT NULL, task_id TEXT NOT NULL,
                arm TEXT NOT NULL, slot INTEGER NOT NULL, request_sha256 TEXT NOT NULL,
                model TEXT NOT NULL, effort TEXT NOT NULL, held_usd_micros INTEGER NOT NULL,
                input_bound INTEGER NOT NULL, output_limit INTEGER NOT NULL,
                input_rate INTEGER NOT NULL, output_rate INTEGER NOT NULL,
                held_at_ns INTEGER NOT NULL, settled_at_ns INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER,
                settled_usd_micros INTEGER, scope_breach INTEGER NOT NULL DEFAULT 0,
                UNIQUE (allocation, task_id, arm, slot)
            )""")
        ledger._run_id = run_id
        return ledger

    @classmethod
    def open(cls, path):
        """Resume the same database. Missing, empty or mismatched state fails closed."""
        ledger = cls(private_path(path, existing=True))
        with ledger._transaction() as db:
            ledger._run_id = db.execute("SELECT run_id FROM pilot WHERE id = 1").fetchone()[0]
        return ledger

    @contextmanager
    def _transaction(self, *, write=False, initialize=False):
        path = private_path(self._path, existing=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10,
                                     isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            require(db.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                    "pilot ledger requires SQLite rollback journaling")
            db.execute("PRAGMA synchronous = EXTRA")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize:
                row = db.execute("SELECT run_id, pilot_sha256, corpus_sha256 FROM pilot WHERE id = 1").fetchone()
                require(row is not None and tuple(row)[1:] == self._binding
                        and (self._run_id is None or row["run_id"] == self._run_id),
                        "pilot ledger belongs to another run, plan or corpus")
            yield db
            db.commit()

    def _trial(self, task_id, arm, call):
        require(task_id in self._plan["tasks"] and arm in self._plan["arms"]
                and bounded_int(call, 1, self._plan["max_calls_per_trial"]),
                "request is outside the frozen pilot trial or call scope")

    def bind_coordinator(self, configuration_sha256):
        check_digest(configuration_sha256)
        with self._transaction(write=True) as db:
            db.execute("""CREATE TABLE coordinator (
                id INTEGER PRIMARY KEY CHECK (id = 1), configuration_sha256 TEXT NOT NULL
            )""")
            db.execute("INSERT INTO coordinator (id, configuration_sha256) VALUES (1, ?)",
                       (configuration_sha256,))

    def verify_coordinator(self, configuration_sha256):
        with self._transaction() as db:
            row = db.execute("SELECT configuration_sha256 FROM coordinator WHERE id = 1").fetchone()
            require(row is not None and row["configuration_sha256"] == configuration_sha256,
                    "coordinator configuration differs from the frozen ledger binding")

    @staticmethod
    def _create_repair_receipts(db):
        db.execute("""CREATE TABLE IF NOT EXISTS repair_receipts (
            reservation_id TEXT PRIMARY KEY REFERENCES reservations(id),
            response_sha256 TEXT NOT NULL, proposal_sha256 TEXT NOT NULL,
            grade_sha256 TEXT
        )""")

    def reserve_repair(self, task_id, arm, call, *, model, effort, input_bound, output_limit,
                       input_rate, output_rate, request_sha256):
        """Hold before one API dispatch, using the bound of the final serialized request."""
        self._trial(task_id, arm, call)
        candidates = {(row["model"], row["effort"]): row for row in self._plan["candidates"]}
        require(isinstance(model, str) and isinstance(effort, str), "invalid repair model or effort")
        target = (model, effort)
        candidate = candidates.get(target)
        require(candidate is not None and candidate["provider"] == "deepseek"
                and target in FIXED_ARMS.values() and (arm == JEV_ARM or target == FIXED_ARMS[arm]),
                "repair model or effort is outside the frozen arm")
        require(bounded_int(input_bound, 1, self._plan["max_input_tokens"])
                and bounded_int(output_limit, self._plan["max_output_tokens"], self._plan["max_output_tokens"]),
                "repair request exceeds its frozen input or output allowance")
        require(type(input_rate) is int and input_rate == micros(candidate["input_usd_per_million"])
                and type(output_rate) is int and output_rate == micros(candidate["output_usd_per_million"]),
                "repair rates differ from the frozen price bounds")
        hold = token_cost(input_bound, output_limit, input_rate, output_rate)
        return self._reserve("repair", task_id, arm, call, request_sha256, hold,
                             model, effort, input_bound, output_limit, input_rate, output_rate)

    def reserve_decision(self, task_id, call, *, model, input_bound, input_rate, request_sha256):
        """Hold for a prepared Jev Choice request using its input-only billing bound."""
        self._trial(task_id, JEV_ARM, call)
        framing = self._plan["decision_input_framing_tokens"]
        require(model == self._plan["router_model"]
                and type(input_rate) is int and input_rate == micros(self._plan["decision_input_usd_per_million"])
                and bounded_int(input_bound, framing + 1, self._plan["max_decision_input_bytes"] + framing),
                "decision identity, input bound or price differs from the frozen scope")
        hold = token_cost(input_bound, 0, input_rate, 0)
        return self._reserve("decisions", task_id, JEV_ARM, call, request_sha256, hold,
                             model, "", input_bound, 0, input_rate, 0)

    def reserve_fleet(self, slot, *, ceiling_usd_micros, configuration_sha256):
        """Hold one all-in fleet cap. The launcher must enforce the cap for every API role."""
        cap = micros(self._plan["usd_per_fleet"])
        require(bounded_int(slot, 1, self._plan["max_fleets"])
                and bounded_int(ceiling_usd_micros, cap, cap),
                "fleet slot or all-in ceiling differs from the frozen scope")
        return self._reserve("fleet_development", "", "", slot, configuration_sha256, cap)

    def verify_fleet_hold(self, slot, configuration_sha256):
        """Verify the retained outer hold before resuming the same native run."""
        check_digest(configuration_sha256)
        with self._transaction() as db:
            hold = db.execute("""SELECT request_sha256, held_usd_micros FROM reservations
                WHERE allocation = 'fleet_development' AND task_id = '' AND arm = '' AND slot = ?""",
                (slot,)).fetchone()
            require(hold is not None and hold["request_sha256"] == configuration_sha256
                    and hold["held_usd_micros"] == micros(self._plan["usd_per_fleet"]),
                    "fleet configuration does not match its retained hold")
            require(db.execute("SELECT 1 FROM reservations WHERE scope_breach = 1 LIMIT 1").fetchone() is None,
                    "a verified provider observation exceeded the frozen scope")

    def _reserve(self, allocation, task_id, arm, slot, digest, hold,
                 model="", effort="", input_bound=0, output_limit=0, input_rate=0, output_rate=0):
        check_digest(digest)
        reservation = Reservation(uuid.uuid4().hex, hold)
        with self._transaction(write=True) as db:
            require(db.execute("SELECT 1 FROM reservations WHERE scope_breach = 1 LIMIT 1").fetchone() is None,
                    "a verified provider observation exceeded the frozen scope")
            scope = (allocation, task_id, arm, slot)
            lookup = "SELECT settled_usd_micros FROM reservations WHERE allocation = ? AND task_id = ? AND arm = ? AND slot = ?"
            require(db.execute(lookup, scope).fetchone() is None,
                    "pilot slot already reserved; repeating dispatch is unsafe")
            prerequisites = []
            if allocation in {"repair", "decisions"} and slot > 1:
                prerequisites.append(("repair", task_id, arm, slot - 1))
            if allocation == "repair" and arm == JEV_ARM:
                prerequisites.append(("decisions", task_id, arm, slot))
            for prerequisite in prerequisites:
                row = db.execute(lookup, prerequisite).fetchone()
                require(row is not None and row[0] is not None,
                        "required preceding decision or repair remains unsettled")
            used = db.execute("""SELECT COALESCE(SUM(COALESCE(settled_usd_micros, held_usd_micros)), 0)
                                FROM reservations WHERE allocation = ?""", (allocation,)).fetchone()[0]
            require(used + hold <= self._caps[allocation], "pilot allocation cannot cover the hold")
            db.execute("""INSERT INTO reservations (
                id, allocation, task_id, arm, slot, request_sha256, model, effort, held_usd_micros,
                input_bound, output_limit, input_rate, output_rate, held_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (reservation.id, *scope, digest, model, effort, hold, input_bound, output_limit,
                        input_rate, output_rate, time.time_ns()))
        return reservation

    def settle_usage(self, reservation_id, *, request_sha256, model, input_tokens, output_tokens,
                     latency_ms=None, repair_receipt=None):
        """Settle independently verified, complete usage; omit settlement for ambiguous dispatches."""
        check_digest(request_sha256)
        if repair_receipt is not None:
            response_sha256, proposal_sha256 = repair_receipt
            check_digest(response_sha256)
            check_digest(proposal_sha256)
        require(bounded_int(input_tokens, 1, MAX_MEASUREMENT) and bounded_int(output_tokens, 0, MAX_MEASUREMENT)
                and (latency_ms is None or bounded_int(latency_ms, 0, MAX_MEASUREMENT)),
                "provider usage or latency is incomplete or malformed")
        with self._transaction(write=True) as db:
            row = db.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
            require(row is not None and row["allocation"] in {"repair", "decisions"}
                    and row["request_sha256"] == request_sha256,
                    "usage does not bind to a pilot model reservation")
            require(row["settled_at_ns"] is None and not row["scope_breach"],
                    "pilot reservation already received a verified observation")
            require(repair_receipt is None or row["allocation"] == "repair",
                    "only a repair response can supply a proposal")
            breach = (row["model"] != model or input_tokens > row["input_bound"]
                      or (row["allocation"] == "repair" and output_tokens > row["output_limit"]))
            # An out-of-scope provider observation freezes further admissions and leaves the hold charged.
            charge = None if breach else token_cost(input_tokens, output_tokens, row["input_rate"], row["output_rate"])
            db.execute("""UPDATE reservations SET input_tokens = ?, output_tokens = ?, latency_ms = ?,
                          settled_usd_micros = ?, settled_at_ns = ?, scope_breach = ? WHERE id = ?""",
                       (input_tokens, output_tokens, latency_ms, charge, time.time_ns(), breach, reservation_id))
            if not breach and repair_receipt is not None:
                self._create_repair_receipts(db)
                db.execute("""INSERT INTO repair_receipts (reservation_id, response_sha256, proposal_sha256)
                              VALUES (?, ?, ?)""", (reservation_id, response_sha256, proposal_sha256))
        require(not breach, "verified provider model or usage exceeded the frozen scope")
        return charge

    def record_repair_grade(self, task, arm, call, *, request_sha256, proposal_sha256, grade_sha256):
        """Bind a reviewed grade to the specific settled response and extracted proposal."""
        self._trial(task, arm, call)
        for digest in (request_sha256, proposal_sha256, grade_sha256):
            check_digest(digest)
        with self._transaction(write=True) as db:
            self._create_repair_receipts(db)
            row = db.execute("""SELECT r.id, c.grade_sha256 FROM reservations AS r
                JOIN repair_receipts AS c ON c.reservation_id = r.id
                WHERE r.allocation = 'repair' AND r.task_id = ? AND r.arm = ? AND r.slot = ?
                  AND r.request_sha256 = ? AND r.settled_usd_micros IS NOT NULL
                  AND r.scope_breach = 0 AND c.proposal_sha256 = ?""",
                (task, arm, call, request_sha256, proposal_sha256)).fetchone()
            require(row is not None and row["grade_sha256"] in (None, grade_sha256),
                    "grade does not match a settled repair proposal")
            db.execute("UPDATE repair_receipts SET grade_sha256 = ? WHERE reservation_id = ?",
                       (grade_sha256, row["id"]))

    def summary(self):
        """Return body-free usage and spend totals. Unresolved holds count in full."""
        with self._transaction() as db:
            return self._summary(db)

    def reporting_snapshot(self):
        """Read the private trial join and public usage in the same transaction."""
        with self._transaction() as db:
            receipts_exist = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'repair_receipts'").fetchone()
            fields = ("r.allocation, r.task_id, r.arm, r.slot, r.request_sha256, "
                      "r.settled_usd_micros IS NOT NULL AS settled")
            source = "reservations AS r"
            if receipts_exist:
                fields += ", c.proposal_sha256, c.grade_sha256"
                source += " LEFT JOIN repair_receipts AS c ON c.reservation_id = r.id"
            rows = db.execute(f"SELECT {fields} FROM {source} WHERE r.allocation IN ('repair', 'decisions')")
            return self._run_id, self._summary(db), [dict(row) for row in rows]

    def _summary(self, db):
        query = """SELECT COUNT(*) AS reservations, COUNT(settled_usd_micros) AS settled,
            COALESCE(SUM(settled_usd_micros), 0) AS settled_usd_micros,
            COALESCE(SUM(CASE WHEN settled_usd_micros IS NULL THEN held_usd_micros END), 0) AS unresolved_usd_micros,
            COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COUNT(latency_ms) AS timed_calls, COALESCE(SUM(latency_ms), 0) AS total_latency_ms,
            COALESCE(SUM(scope_breach), 0) AS scope_breaches FROM reservations WHERE allocation = ?"""
        scope_breach = db.execute("SELECT 1 FROM reservations WHERE scope_breach = 1 LIMIT 1").fetchone() is not None
        allocations = {}
        for allocation, cap in self._caps.items():
            values = dict(db.execute(query, (allocation,)).fetchone())
            charged = values["settled_usd_micros"] + values["unresolved_usd_micros"]
            available = cap - charged if allocation != "undispatched" and not scope_breach else 0
            allocations[allocation] = {**values, "ceiling_usd_micros": cap, "charged_usd_micros": charged,
                                       "unresolved": values["reservations"] - values["settled"],
                                       "available_usd_micros": available}
        return {"schema": "doxa-pilot-usage-v1", "pilot_sha256": self._binding[0],
                "corpus_sha256": self._binding[1], "scope_breach": scope_breach, "allocations": allocations,
                "authorized_usd_micros": sum(self._caps.values()),
                "charged_usd_micros": sum(row["charged_usd_micros"] for row in allocations.values())}
