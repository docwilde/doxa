# SPDX-License-Identifier: AGPL-3.0-only
"""Peer ledger tests -- named for the failure each one catches.

The ledger is an instrument (docs/plans/emergent-organization.md): a record
it loses, mangles or reorders is a measurement that cannot be taken again,
because the sessions that produced it are gone. So the tests here are less
about API surface than about the four properties the experiment rests on --
a secret never reaches disk while identical messages still hash identically,
a reader never sees half a line, the ceiling refuses instead of rotating,
and a broadcast is priced per recipient rather than per call.

Every test owns its own file under tmp_path; nothing here reads or writes
the machine's real ledger (conftest.py additionally pins DOXA_HOME to a
throwaway directory for the whole suite).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone

import pytest

from doxa import peerledger as pl

# The same shapes doxa's other trust tests use: a real AWS key pattern and a
# key=value secret, both of which lore_core.scrub redacts.
FAKE_AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
FAKE_KV_SECRET = "api_key=supersecretvalue123"

FIXED = datetime(2026, 9, 17, 19, 32, 0, 123456, tzinfo=timezone.utc)


def _prose(length):
    """A long body that is not a credential shape. An unbroken run of
    base64-alphabet characters ("x" * 100_000) is one, so a test built on
    one measures lore_core.scrub's redaction (and its quadratic cost on
    runs -- 39 s at 100 KB, measured) rather than the ledger."""
    return ("the quick brown fox jumps over the lazy dog " * (length // 43 + 1))[:length]


def _sender(session="s-alpha", **kwargs):
    return pl.Sender(
        session=session,
        title=kwargs.get("title", "refactor the parser"),
        repo=kwargs.get("repo", "/abs/repo"),
        model=kwargs.get("model", "claude-opus-5"),
        engine=kwargs.get("engine", "claude"),
    )


def _ledger(tmp_path, **kwargs):
    return pl.PeerLedger(tmp_path / "peers" / "messages.jsonl", **kwargs)


def _append(ledger, body="hello", to=("s-beta",), **kwargs):
    return ledger.append(sender=kwargs.pop("sender", _sender()), to=to, body=body, **kwargs)


# -- the body: scrubbed on disk, hashed before it was ------------------


def test_a_credential_in_a_body_is_scrubbed_before_it_reaches_disk(tmp_path):
    """The failure this catches is the worst one available: a peer message
    quoting a key, written verbatim into a file that outlives the run."""
    ledger = _ledger(tmp_path)
    raw = f"deploy with {FAKE_AWS_KEY} and {FAKE_KV_SECRET}"

    message = ledger.append(sender=_sender(), to=["s-beta"], body=raw)

    on_disk = ledger.path.read_text(encoding="utf-8")
    assert FAKE_AWS_KEY not in on_disk
    assert "supersecretvalue123" not in on_disk
    assert "[REDACTED" in message.body
    # ...and the scrub happened on the way to disk, not only in the return
    # value: what a later reader gets back is the scrubbed text too.
    assert FAKE_AWS_KEY not in ledger.recent(1)[0].body


def test_the_hash_is_of_the_body_before_scrubbing_not_after(tmp_path):
    """If the hash were taken after the scrub, two messages that differed
    only inside a redacted span would collide -- and, worse, the hash would
    describe DOXA's redaction rather than what the agent said."""
    ledger = _ledger(tmp_path)
    raw = f"deploy with {FAKE_AWS_KEY}"

    message = ledger.append(sender=_sender(), to=["s-beta"], body=raw)

    assert message.body_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert message.body_sha256 != hashlib.sha256(message.body.encode("utf-8")).hexdigest()
    # Survives the round trip: the hash is a stored field, not a derivation.
    assert ledger.recent(1)[0].body_sha256 == message.body_sha256


def test_two_identical_bodies_hash_identically_after_scrubbing(tmp_path):
    """This is how an agent (or the analysis) recognises a loop. If the two
    hashes ever differ, a repeating message stops being detectable exactly
    when it matters most."""
    ledger = _ledger(tmp_path)
    raw = f"status? {FAKE_AWS_KEY}"

    first = ledger.append(sender=_sender("s-alpha"), to=["s-beta"], body=raw)
    second = ledger.append(sender=_sender("s-gamma"), to=["s-beta"], body=raw)
    different = ledger.append(sender=_sender("s-gamma"), to=["s-beta"], body=raw + "!")

    assert first.body_sha256 == second.body_sha256
    assert first.body != raw and first.body == second.body
    assert first.id != second.id
    assert different.body_sha256 != first.body_sha256


def test_a_body_is_stored_in_full_and_never_truncated(tmp_path):
    """The content IS the measurement -- a coordination message and a status
    ping are distinguished by nothing else."""
    ledger = _ledger(tmp_path)
    body = _prose(200_000)

    message = ledger.append(sender=_sender(), to=["s-beta"], body=body)

    assert message.body == body
    assert ledger.recent(1)[0].body == body


def test_a_blob_body_is_redacted_which_is_what_stored_in_full_means(tmp_path):
    """"Stored in full" means the full SCRUBBED body, and for a long
    base64-alphabet run the scrubber's answer is a short token -- it cannot
    tell a minified bundle from a key. Recorded here so the experiment reads
    a redacted blob as scrubbing rather than as truncation."""
    ledger = _ledger(tmp_path)
    blob = "QWxpY2VCb2JDaGFybGll" * 50  # 1 KB of base64 alphabet, no spaces

    message = ledger.append(sender=_sender(), to=["s-beta"], body=blob)

    assert "[REDACTED" in message.body and len(message.body) < 100
    # The hash is still of what was actually sent, so an agent repeating the
    # same blob is still detectable as a repeat.
    assert message.body_sha256 == hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_an_identifier_is_never_mangled_by_the_scrubber(tmp_path):
    """Session ids and recipient ids are identity, not prose. A scrubber
    that rewrote one would break every join in the analysis."""
    ledger = _ledger(tmp_path)
    session = "a" * 64  # a hex-run shape the scrubber would redact in prose

    message = ledger.append(
        sender=pl.Sender(session=session), to=[session[::-1]], body="hi"
    )

    assert message.sender.session == session
    assert message.to == (session[::-1],)


def test_a_secret_in_the_senders_own_title_is_scrubbed_too(tmp_path):
    """A session's title comes from its first prompt, and a prompt can carry
    a credential -- so the sender block is scrubbed even though it is
    first-party."""
    ledger = _ledger(tmp_path)

    message = ledger.append(
        sender=_sender(title=f"fix {FAKE_AWS_KEY}"), to=["s-beta"], body="hi"
    )

    assert FAKE_AWS_KEY not in (message.sender.title or "")
    assert FAKE_AWS_KEY not in ledger.path.read_text(encoding="utf-8")


# -- the record shape, which two other components are built against ---


def test_the_record_matches_the_contract_key_for_key(tmp_path):
    """A contract lock. Anything that changes a key name or a type here
    breaks readers that were written against the published shape, and this
    test is where that shows up rather than in their code."""
    ledger = _ledger(tmp_path, now=lambda: FIXED)
    first = ledger.append(sender=_sender(), to=["s-beta"], body="one")

    message = ledger.append(
        sender=_sender(),
        to=["s-beta", "s-gamma"],
        body="two",
        kind="broadcast",
        in_reply_to=first.id,
        latency_ms=1234,
        turn=pl.TurnRef(id="9f3c1d2e4b5a", state="running"),
    )

    line = ledger.path.read_text(encoding="utf-8").splitlines()[-1]
    obj = json.loads(line)
    assert list(obj) == [
        "v", "id", "ts", "from", "to", "kind", "in_reply_to", "body",
        "body_sha256", "latency_ms", "turn",
    ]
    assert obj["v"] == 1
    assert obj["id"] == message.id and len(obj["id"]) == 32
    assert obj["ts"] == "2026-09-17T19:32:00.123457Z"  # stamped, sub-second, Z
    assert obj["from"] == {
        "session": "s-alpha", "title": "refactor the parser",
        "repo": "/abs/repo", "model": "claude-opus-5", "engine": "claude",
    }
    assert obj["to"] == ["s-beta", "s-gamma"]
    assert obj["kind"] == "broadcast"
    assert obj["in_reply_to"] == first.id
    assert obj["body"] == "two"
    assert obj["body_sha256"] == hashlib.sha256(b"two").hexdigest()
    assert obj["latency_ms"] == 1234
    assert obj["turn"] == {"id": "9f3c1d2e4b5a", "state": "running"}


def test_to_is_a_list_even_for_a_direct_message(tmp_path):
    """So broadcast is the same shape with more entries, and no reader ever
    has to handle a second one."""
    ledger = _ledger(tmp_path)

    ledger.append(sender=_sender(), to=["s-beta"], body="just you")

    obj = json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[0])
    assert obj["to"] == ["s-beta"]
    assert obj["kind"] == "direct"


def test_one_record_stays_on_one_line_however_the_body_is_shaped(tmp_path):
    """A newline inside a body must not become a record boundary."""
    ledger = _ledger(tmp_path)

    ledger.append(sender=_sender(), to=["s-beta"], body="one\ntwo\r\nthree four")

    assert len(ledger.path.read_bytes().splitlines()) == 1
    assert ledger.recent(1)[0].body == "one\ntwo\r\nthree four"


def test_a_record_that_would_break_the_contract_is_refused_at_the_door(tmp_path):
    """Two other components write through this API. A silent coercion here
    is a corrupt ledger nobody notices until the analysis."""
    ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match=r"must be a sequence of session ids"):
        ledger.append(sender=_sender(), to="s-beta", body="hi")
    with pytest.raises(ValueError, match=r"at least one recipient"):
        ledger.append(sender=_sender(), to=[], body="hi")
    with pytest.raises(ValueError, match=r"kind must be one of"):
        ledger.append(sender=_sender(), to=["s-beta"], body="hi", kind="shout")
    with pytest.raises(ValueError, match=r"latency_ms must be"):
        ledger.append(sender=_sender(), to=["s-beta"], body="hi", latency_ms=-1)
    with pytest.raises(ValueError, match=r"non-empty session id"):
        pl.Sender(session="")
    with pytest.raises(ValueError, match=r"turn state must be one of"):
        pl.TurnRef(id="t1", state="thinking")
    assert not ledger.path.exists()  # nothing half-written by any of them


def test_a_repeated_recipient_is_not_counted_as_two_deliveries(tmp_path):
    """A ledger claiming a delivery that never happened inflates in-degree,
    which is one of the measures the experiment reports."""
    ledger = _ledger(tmp_path)

    message = ledger.append(
        sender=_sender(), to=["s-beta", "s-beta", "s-gamma"], body="hi",
        kind="broadcast",
    )

    assert message.to == ("s-beta", "s-gamma")


# -- ordering ---------------------------------------------------------


def test_sub_second_order_is_preserved_across_writes_in_the_same_second(tmp_path):
    """Ordering at one-second resolution is not ordering: a reply-round of
    992 messages lands inside a second, and betweenness computed over a
    ledger that cannot order them is computed over a coin flip."""
    ledger = _ledger(tmp_path)

    written = [_append(ledger, body=f"m{n}") for n in range(25)]

    stamps = [m.ts for m in written]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    # The test only means something if it actually exercised the same-second
    # case, so assert that it did.
    assert len({s[:19] for s in stamps}) < len(stamps)
    # File order is the same order, and it is what a reader gets back.
    assert [m.id for m in ledger.since(None)] == [m.id for m in written]
    assert all(m.ts.endswith("Z") and len(m.ts.split(".")[1]) == 7 for m in written)


def test_a_clock_that_steps_backwards_still_writes_increasing_stamps(tmp_path):
    """NTP and suspend/resume both step a wall clock backwards. A record
    that sorts before its own predecessor is a reordered experiment."""
    ledger = _ledger(tmp_path, now=lambda: FIXED)

    stamps = [_append(ledger, body=f"m{n}").ts for n in range(3)]

    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    assert stamps[0] == "2026-09-17T19:32:00.123456Z"
    assert stamps[2] == "2026-09-17T19:32:00.123458Z"


# -- partial lines: the reader half and the writer half ---------------


def test_a_reader_never_sees_a_partial_line(tmp_path):
    """The deterministic half: bytes without their newline are a record
    still being written, and consuming them would hand a caller a truncated
    body (or, once it parsed, a lie)."""
    ledger = _ledger(tmp_path)
    _append(ledger, body="complete")
    assert len(ledger.snapshot()) == 1

    # A second record, half-flushed.
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write('{"v":1,"id":"deadbeef","ts":"2026-09-17T19:32:00.0000')

    assert len(ledger.snapshot()) == 1
    assert len(ledger.since(None)) == 1

    # ...and the rest of it arriving is not lost: the reader left its
    # offset at the newline, so the completed line is picked up next poll.
    rest = pl.Message(
        id="deadbeef", ts="2026-09-17T19:32:00.000001Z", sender=pl.Sender("s-alpha"),
        to=("s-beta",), kind="direct", body="the other half", body_sha256="",
    )
    with ledger.path.open("w", encoding="utf-8") as handle:
        handle.write(ledger.path.read_text(encoding="utf-8").split("\n")[0] + "\n")
        handle.write(rest.to_line() + "\n")
    assert [m.id for m in ledger.snapshot()][-1] == "deadbeef"


def test_a_reader_polling_mid_write_sees_the_record_whole_or_not_at_all(
    tmp_path, monkeypatch
):
    """The other half, driven deterministically: a large body takes more
    than one write() even under the lock, so the file genuinely holds half a
    record for a moment. A reader that polls exactly then must see the
    record it can trust -- the previous one -- and never the half."""
    ledger = _ledger(tmp_path)
    _append(ledger, body="first")

    observed = []
    real_write_all = pl._write_all

    def split_write(fd, data):
        real_write_all(fd, data[: len(data) // 2])
        observed.append(ledger.snapshot())          # the poll, mid-record
        observed.append(list(ledger.since(None)))
        real_write_all(fd, data[len(data) // 2:])

    monkeypatch.setattr(pl, "_write_all", split_write)
    body = _prose(100_000)
    second = _append(ledger, body=body)

    assert observed, "the split write never ran -- this test proved nothing"
    for view in observed:
        assert [m.body for m in view] == ["first"]
    final = ledger.snapshot()
    assert [m.id for m in final][-1] == second.id
    assert final[-1].body == body and len(final) == 2


def test_concurrent_writers_never_interleave_their_bytes(tmp_path):
    """Two appends that interleave produce two unreadable lines and lose
    both records. flock is what prevents it; this is the test that notices
    if it is ever dropped."""
    ledger = _ledger(tmp_path)
    barrier = threading.Barrier(5)

    def writer(index):
        barrier.wait()
        for n in range(20):
            _append(ledger, body=f"{index}-{n}-" + _prose(5_000))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    parsed = [pl.Message.parse_line(line) for line in lines]  # raises if interleaved
    assert len({m.id for m in parsed}) == 100
    assert ledger.malformed_lines == 0
    assert [m.ts for m in parsed] == sorted(m.ts for m in parsed)


# -- the ceiling ------------------------------------------------------


def test_the_ceiling_refuses_and_says_so_rather_than_rotating(tmp_path):
    """Rotation is the wrong response to pathology: hitting the ceiling
    means something is wrong and this file is the evidence, and a log that
    discards its oldest half destroys the beginning of the runaway that
    would explain it."""
    ledger = _ledger(tmp_path, ceiling_bytes=1_200)
    kept = []
    with pytest.raises(pl.LedgerFull, match=r"REFUSED and nothing was rotated") as caught:
        for n in range(50):  # bounded: the ceiling must stop this, not the range
            kept.append(_append(ledger, body=f"record {n}"))
            size_before = ledger.path.stat().st_size
    assert 1 < len(kept) < 50

    error = caught.value
    assert error.path == ledger.path
    assert error.ceiling_bytes == 1_200
    assert error.size_bytes == size_before and error.needed_bytes > 0
    assert str(ledger.path) in str(error)
    # Nothing rotated, nothing dropped, nothing truncated.
    assert sorted(p.name for p in ledger.path.parent.iterdir()) == ["messages.jsonl"]
    assert ledger.path.stat().st_size == size_before
    assert [m.id for m in ledger.since(None)] == [m.id for m in kept]


def test_the_ceiling_is_far_above_anything_the_experiment_produces(tmp_path):
    """The number itself is load-bearing: a ceiling normal use approaches is
    a ceiling that refuses a healthy run. Measured in the plan: ~4.5 MB per
    N=32 reply-round, ten rounds a run, twenty runs in the experiment."""
    whole_experiment = 20 * 10 * 4.5 * 1024 * 1024

    assert pl.MAX_LEDGER_BYTES > whole_experiment * 2
    assert pl.PeerLedger(tmp_path / "x.jsonl").ceiling_bytes == pl.MAX_LEDGER_BYTES


def test_headroom_is_readable_before_the_wall_is_hit(tmp_path):
    """So a surface can warn while there is still something to do about
    it, rather than only reporting the refusal."""
    ledger = _ledger(tmp_path, ceiling_bytes=1_200)
    assert ledger.headroom_bytes() == 1_200 and not ledger.is_full()

    _append(ledger, body="a" * 100)

    assert 0 < ledger.headroom_bytes() < 1_200


# -- permissions ------------------------------------------------------


def test_the_ledger_is_private_to_the_user_who_wrote_it(tmp_path):
    """Full message bodies from every session on the machine: same-user,
    enforced by the filesystem rather than by protocol."""
    ledger = _ledger(tmp_path)
    _append(ledger)

    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700


def test_a_ledger_left_world_readable_is_clamped_on_the_next_append(tmp_path):
    """O_CREAT does not change an existing file's mode, so a file created
    before this rule (or by a careless hand) would stay readable forever."""
    ledger = _ledger(tmp_path)
    _append(ledger)
    os.chmod(ledger.path, 0o644)

    _append(ledger)

    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600


def test_the_default_path_follows_doxa_home(tmp_path, monkeypatch):
    """A harness gives each run its own DOXA_HOME; that is how a run's
    ledger is collected without filtering anything."""
    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home-one"))
    assert pl.ledger_path() == tmp_path / "home-one" / "peers" / "messages.jsonl"
    first = pl.ledger()

    monkeypatch.setenv("DOXA_HOME", str(tmp_path / "home-two"))
    assert pl.ledger() is not first
    assert pl.ledger().path == tmp_path / "home-two" / "peers" / "messages.jsonl"


# -- queries ----------------------------------------------------------


def test_sent_and_received_come_back_newest_first_within_the_limit(tmp_path):
    """What a graph poll asks for. Oldest-first here would mean a reader
    with a limit sees the beginning of the run forever."""
    ledger = _ledger(tmp_path)
    for n in range(5):
        ledger.append(sender=_sender("s-alpha"), to=["s-beta"], body=f"a{n}")
    for n in range(3):
        ledger.append(sender=_sender("s-beta"), to=["s-alpha", "s-gamma"],
                      body=f"b{n}", kind="broadcast")

    sent = ledger.sent_by("s-alpha", limit=3)
    received = ledger.received_by("s-alpha", limit=10)

    assert [m.body for m in sent] == ["a4", "a3", "a2"]
    assert [m.body for m in received] == ["b2", "b1", "b0"]
    # A broadcast counts for every recipient it named -- in-degree without
    # a second index.
    assert [m.body for m in ledger.received_by("s-gamma", limit=10)] == ["b2", "b1", "b0"]
    assert ledger.received_by("s-nobody", limit=10) == []


def test_a_query_reaches_past_the_records_still_held_in_memory(tmp_path):
    """The resident cache is an optimisation, not the answer. A query it
    cannot satisfy must fall back to the file rather than report less."""
    ledger = _ledger(tmp_path, cache_records=2)
    for n in range(8):
        ledger.append(sender=_sender("s-alpha"), to=["s-beta"], body=f"a{n}")

    assert [m.body for m in ledger.recent(5)] == ["a7", "a6", "a5", "a4", "a3"]
    assert [m.body for m in ledger.sent_by("s-alpha", limit=8)][-1] == "a0"
    assert ledger.count() == 8


def test_since_returns_only_what_arrived_after_the_cursor(tmp_path):
    """The incremental form, oldest first: a reader appends what it gets to
    what it already has."""
    ledger = _ledger(tmp_path)
    first = _append(ledger, body="one")
    cursor = _append(ledger, body="two")
    later = [_append(ledger, body=b) for b in ("three", "four")]

    assert [m.body for m in ledger.since(cursor.id)] == ["three", "four"]
    assert [m.body for m in ledger.since(None)] == ["one", "two", "three", "four"]
    assert [m.body for m in ledger.since(later[-1].id)] == []
    assert [m.body for m in ledger.since(first.id, limit=1)] == ["two"]
    assert ledger.latest_id() == later[-1].id


def test_since_can_narrow_to_one_sessions_view(tmp_path):
    """An incremental reader for one session wants that session's edges,
    not the fleet's."""
    ledger = _ledger(tmp_path)
    cursor = _append(ledger, body="zero")
    ledger.append(sender=_sender("s-alpha"), to=["s-beta"], body="to beta")
    ledger.append(sender=_sender("s-gamma"), to=["s-delta"], body="elsewhere")
    ledger.append(sender=_sender("s-delta"), to=["s-alpha"], body="to alpha")

    seen = ledger.since(cursor.id, session_id="s-alpha")

    assert [m.body for m in seen] == ["to beta", "to alpha"]


def test_since_refuses_a_cursor_this_ledger_does_not_hold(tmp_path):
    """Answering "everything" would flood a reader whose cursor went
    missing with the one answer it was not asking for."""
    ledger = _ledger(tmp_path)
    _append(ledger)

    with pytest.raises(pl.UnknownCursor, match=r"Re-bootstrap from latest_id"):
        ledger.since("f" * 32)


def test_since_still_answers_a_cursor_older_than_the_cache(tmp_path):
    """The eviction boundary is invisible to callers or it is a bug that
    only appears under load."""
    ledger = _ledger(tmp_path, cache_records=2)
    cursor = _append(ledger, body="zero")
    for n in range(6):
        _append(ledger, body=f"m{n}")

    assert [m.body for m in ledger.since(cursor.id)] == [f"m{n}" for n in range(6)]


def test_a_poll_parses_only_what_arrived_since_the_last_one(tmp_path, monkeypatch):
    """A browser graph polls a few times a second. Re-parsing the whole file
    per call is the one thing the reader may not do, and nothing about the
    returned values would reveal that it had started."""
    ledger = _ledger(tmp_path)
    for n in range(20):
        _append(ledger, body=f"m{n}")
    ledger.snapshot()

    parsed = []
    real = pl.Message.parse_line
    monkeypatch.setattr(
        pl.Message, "parse_line",
        staticmethod(lambda line: (parsed.append(line), real(line))[1]),
    )
    ledger.snapshot()
    assert parsed == []          # nothing new: nothing parsed

    _append(ledger, body="fresh")
    view = ledger.snapshot()

    assert len(parsed) == 1      # one new line, one parse -- not 21
    assert len(view) == 21 and view[-1].body == "fresh"


def test_a_replaced_ledger_file_is_re_read_from_the_start(tmp_path):
    """Archiving the file (the only sanctioned way to clear it) must not
    leave a long-lived reader reporting records that are no longer there."""
    ledger = _ledger(tmp_path)
    _append(ledger, body="old")
    assert len(ledger.snapshot()) == 1

    ledger.path.replace(ledger.path.with_suffix(".archived"))
    _append(ledger, body="new")

    view = ledger.snapshot()
    assert [m.body for m in view] == ["new"]
    assert ledger.count() == 1


def test_a_malformed_line_is_counted_not_fatal(tmp_path):
    """One bad line must cost one record, never the reader. A UI that says
    "1 unreadable record" is honest; one that silently draws fewer edges is
    not."""
    ledger = _ledger(tmp_path)
    _append(ledger, body="good")
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write('{"v":1,"id":"x","to":"not-a-list","kind":"direct","body":"b"}\n')
    _append(ledger, body="also good")

    view = ledger.snapshot()

    assert [m.body for m in view] == ["good", "also good"]
    assert ledger.malformed_lines == 2


# -- the rate limit ---------------------------------------------------


def test_one_broadcast_to_many_peers_costs_one_delivery_per_peer(tmp_path):
    """Counting calls instead of deliveries makes a single call an unbounded
    amplifier: at N=32 one broadcast is 31 messages, and a limit that prices
    it as one is a decoration."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=64, per_window=512))
    peers = [f"s-{n}" for n in range(31)]

    first = limiter.charge(recipients=peers, turn_id="t1", now=FIXED)
    assert first.allowed and first.fanout == 31
    assert limiter.used_in_turn("t1") == 31

    second = limiter.charge(recipients=peers, turn_id="t1", now=FIXED)
    assert second.allowed
    assert limiter.used_in_turn("t1") == 62

    third = limiter.charge(recipients=peers, turn_id="t1", now=FIXED)
    assert not third.allowed and third.scope == "turn"
    assert limiter.used_in_turn("t1") == 62      # a refusal spends nothing
    assert "31" in (third.reason or "") and "64" in (third.reason or "")


def test_a_refusal_names_when_the_budget_resets(tmp_path):
    """An agent told why it was refused can reason about it -- reply to
    fewer peers, wait, stop. One that is silently throttled just retries,
    which is the behaviour the limit exists to prevent."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=100, per_window=10, window_secs=60))
    limiter.charge(recipients=[f"s-{n}" for n in range(8)], turn_id="t1", now=FIXED)

    refused = limiter.charge(
        recipients=["s-a", "s-b", "s-c"], turn_id="t2", now=FIXED + timedelta(seconds=5)
    )

    assert not refused.allowed and refused.scope == "window"
    expected_reset = FIXED + timedelta(seconds=60)
    assert refused.reset_at == expected_reset
    assert refused.retry_after_secs == pytest.approx(55.0)
    assert pl.format_ts(expected_reset) in (refused.reason or "")
    assert "55.0 s" in (refused.reason or "")
    assert refused.reset_description() == f"at {pl.format_ts(expected_reset)} (in 55.0 s)"
    # ...and loudly: raising is one call away, carrying the same words.
    with pytest.raises(pl.SendRefused, match=r"frees up at 2026-09-17"):
        refused.raise_if_refused()


def test_a_per_turn_refusal_names_the_turn_boundary_it_waits_on(tmp_path):
    """Nothing knows when the current turn ends, so the refusal names the
    event instead of inventing a time -- a wrong clock time would teach an
    agent to ignore the field."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=10, per_window=500))
    limiter.charge(recipients=[f"s-{n}" for n in range(9)], turn_id="t-9f3c", now=FIXED)

    refused = limiter.charge(recipients=["s-a", "s-b"], turn_id="t-9f3c", now=FIXED)

    assert not refused.allowed and refused.scope == "turn"
    assert refused.reset_at is None
    assert "resets when turn t-9f3c ends" in (refused.reason or "")
    assert refused.reset_description() == "when turn t-9f3c ends"


def test_the_window_frees_up_at_exactly_the_time_the_refusal_promised(tmp_path):
    """A reset time that is wrong in the impatient direction is worse than
    none: the agent retries, is refused again, and stops believing the
    field."""
    limits = pl.SendLimits(per_turn=100, per_window=10, window_secs=60)
    limiter = pl.RateLimiter(limits)
    limiter.charge(recipients=[f"s-{n}" for n in range(8)], turn_id="t1", now=FIXED)
    refused = limiter.charge(recipients=["s-a", "s-b", "s-c"], turn_id="t2",
                             now=FIXED + timedelta(seconds=5))

    just_before = limiter.check(recipients=["s-a", "s-b", "s-c"], turn_id="t2",
                                now=refused.reset_at - timedelta(milliseconds=1))
    just_after = limiter.check(recipients=["s-a", "s-b", "s-c"], turn_id="t2",
                               now=refused.reset_at + timedelta(milliseconds=1))

    assert not just_before.allowed
    assert just_after.allowed


def test_the_reset_waits_for_enough_budget_not_merely_the_oldest_delivery(tmp_path):
    """With a fan-out of 5 against a budget that is 3 short, the oldest
    single delivery ageing out is still a refusal."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=100, per_window=10, window_secs=60))
    limiter.charge(recipients=["s-a"], turn_id="t1", now=FIXED)
    limiter.charge(recipients=["s-b", "s-c", "s-d", "s-e", "s-f", "s-g", "s-h"],
                   turn_id="t1", now=FIXED + timedelta(seconds=10))

    refused = limiter.check(recipients=[f"s-{n}" for n in range(5)], turn_id="t2",
                            now=FIXED + timedelta(seconds=20))

    # Losing the single delivery at FIXED leaves 7 + 5 = 12 > 10; the reset
    # is when the SEVEN age out, not when the one does.
    assert refused.reset_at == FIXED + timedelta(seconds=70)


def test_a_fan_out_bigger_than_the_whole_budget_says_it_will_never_fit(tmp_path):
    """Waiting does not help, and a refusal that names a reset time here
    would be a lie."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=10, per_window=500))

    refused = limiter.charge(recipients=[f"s-{n}" for n in range(31)],
                             turn_id="t1", now=FIXED)

    assert not refused.allowed and refused.reset_at is None
    assert "never fit" in (refused.reason or "")
    assert refused.reset_description() == "never, at this fan-out"


def test_another_turns_deliveries_do_not_spend_this_turns_budget(tmp_path):
    """Per-turn means per turn. A budget that leaked across turns would
    throttle the second turn of every session for free."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=10, per_window=500))
    limiter.charge(recipients=[f"s-{n}" for n in range(9)], turn_id="t1", now=FIXED)

    other_turn = limiter.charge(recipients=[f"s-{n}" for n in range(9)],
                                turn_id="t2", now=FIXED)

    assert other_turn.allowed
    assert limiter.used_in_turn("t1") == 9 and limiter.used_in_turn("t2") == 9
    assert limiter.used_in_window(FIXED) == 18


def test_a_send_outside_any_turn_is_still_bounded_by_the_window(tmp_path):
    """A None turn bucket would never reset -- it would refuse every
    out-of-turn send forever while naming a reset that never comes. The
    window does reset, so the window is what bounds it."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=2, per_window=10, window_secs=60))

    for _ in range(5):
        assert limiter.charge(recipients=["s-a", "s-b"], turn_id=None, now=FIXED).allowed
    refused = limiter.charge(recipients=["s-a", "s-b"], turn_id=None, now=FIXED)

    assert not refused.allowed and refused.scope == "window"


def test_the_limiter_prices_a_repeated_recipient_the_way_the_ledger_does(tmp_path):
    """The budget and the record must agree on what a broadcast cost, or
    the ledger cannot be replayed against the limit."""
    limiter = pl.RateLimiter(pl.SendLimits(per_turn=10, per_window=500))

    decision = limiter.charge(recipients=["s-a", "s-a", "s-b"], turn_id="t1", now=FIXED)

    assert decision.fanout == 2


def test_the_decision_is_pure_no_clock_no_file_no_hidden_state(tmp_path):
    """It is a decision function so that it can be tested by calling it --
    and so the same numbers can be replayed offline against a collected
    ledger to ask what a different limit would have done."""
    limits = pl.SendLimits(per_turn=5, per_window=100)
    history = [pl.Delivery(at=FIXED, count=4, turn_id="t1")]

    first = pl.decide_send(limits, history, turn_id="t1", fanout=2, now=FIXED)
    again = pl.decide_send(limits, history, turn_id="t1", fanout=2, now=FIXED)

    assert first == again                      # same inputs, same answer
    assert not first.allowed and first.turn_used == 4
    assert history == [pl.Delivery(at=FIXED, count=4, turn_id="t1")]  # untouched
    assert pl.decide_send(limits, [], turn_id="t1", fanout=5, now=FIXED).allowed
    with pytest.raises(ValueError, match=r"at least one delivery"):
        pl.decide_send(limits, [], turn_id="t1", fanout=0, now=FIXED)


def test_the_default_limits_pass_a_broadcast_at_the_experiments_scale(tmp_path):
    """N=32 is the plan's number: a default that refused a single broadcast
    there would make the primary manipulation unusable."""
    limiter = pl.RateLimiter()
    peers = [f"s-{n}" for n in range(31)]

    assert limiter.charge(recipients=peers, turn_id="t1", now=FIXED).allowed
    # ...and ten rounds of them inside a minute stay inside the window.
    total = 1
    for n in range(9):
        decision = limiter.charge(recipients=peers, turn_id=f"t{n + 2}",
                                  now=FIXED + timedelta(seconds=n + 1))
        total += 1 if decision.allowed else 0
    assert total == 10


# -- async door -------------------------------------------------------


async def test_appending_from_async_code_does_not_hold_the_event_loop(tmp_path):
    """At N=32 an append can wait on 31 other sessions' locks. The async
    door exists so no caller has to remember to wrap it."""
    ledger = _ledger(tmp_path)

    message = await ledger.append_async(sender=_sender(), to=["s-beta"], body="hi")

    assert ledger.recent(1)[0].id == message.id
