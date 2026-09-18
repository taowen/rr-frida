"""Tests for the mechanisms the parent-contract chapter relies on.

    python3 -m unittest rrtrace.tests.test_snapshots
"""

from __future__ import annotations

import json
import struct
import unittest

from rrtrace.format import (
    EVENT_HEADER,
    EVENT_HEADER_SIZE,
    FILE_HEADER,
    FILE_MAGIC,
    PHASES,
    TraceError,
    decode_trace,
    snapshot_parts,
)

# Minimal but valid metadata; the probes below define the snapshots.
METADATA = {
    "schema": 1,
    "session_id": "snap",
    "probe_set": {"name": "snap", "module": "x.so",
                  "agent_summary": {"module": {"probes": []}}},
    "agent_summary": {"module": {"name": "x.so", "probes": []}},
}


def encode_event(kind, phase, tid, sequence, correlation, registers, snapshot=b"",
                 flags=0):
    payload_size = 40 + len(snapshot)
    total_size = (EVENT_HEADER_SIZE + payload_size + 7) & ~7
    body = bytearray(total_size)
    EVENT_HEADER.pack_into(body, 0, total_size, EVENT_HEADER_SIZE, kind, phase, flags,
                           payload_size, tid, sequence, 0, correlation, 0)
    struct.pack_into("<5Q", body, EVENT_HEADER_SIZE, *registers)
    body[EVENT_HEADER_SIZE + 40:EVENT_HEADER_SIZE + 40 + len(snapshot)] = snapshot
    return bytes(body)


def encode_trace(events):
    meta = json.dumps(METADATA).encode()
    pad = (-len(meta)) % 8
    header = FILE_HEADER.pack(FILE_MAGIC, FILE_HEADER.size, 1, 0, 0, 0, 0, 0,
                              len(meta), 0, 0, 0, 0, b"\0" * 8)
    return header + meta + b"\0" * pad + b"".join(events)


def call_events(snapshot, flags, registers=(0, 0, 0, 0, 0), kind=100):
    return [
        encode_event(kind, 1, 0x10, 1, 1, registers, snapshot, flags),
        encode_event(kind, 2, 0x10, 2, 1, registers),
    ]


class TestSnapshots(unittest.TestCase):
    def test_register_snapshot_is_eight_bytes(self):
        # The enter event carries one register snapshot.
        snapshot = struct.pack("<Q", 0x40000000)
        trace = decode_trace(encode_trace(call_events(snapshot, flags=1)))
        definitions = [{"phase": "enter", "register": "s0"}]
        parts = snapshot_parts(trace.calls[1].enter, definitions)
        self.assertEqual(len(parts), 1)
        definition, raw = parts[0]
        self.assertEqual(raw, snapshot)

    def test_dynamic_snapshot_uses_size_argument(self):
        # size_arg=2 means "read arg2 * multiplier bytes". The enter registers
        # are x0..x3; arg2 is 3 here, so 12 bytes.
        payload = b"abcdefghijkl"
        registers = (10, 11, 3, 0, 0)
        trace = decode_trace(encode_trace(call_events(payload, flags=1, registers=registers)))
        definitions = [{"phase": "enter", "source": "arg1", "size_arg": 2,
                        "multiplier": 4, "max": 256}]
        definition, raw = snapshot_parts(trace.calls[1].enter, definitions,
                                         enter=trace.calls[1].enter)[0]
        self.assertEqual(raw, payload)

    def test_dynamic_snapshot_honours_max(self):
        payload = b"x" * 8
        registers = (10, 11, 999, 0, 0)
        trace = decode_trace(encode_trace(call_events(payload, flags=1, registers=registers)))
        definitions = [{"phase": "enter", "source": "arg1", "size_arg": 2,
                        "multiplier": 4, "max": 8}]
        _, raw = snapshot_parts(trace.calls[1].enter, definitions,
                                enter=trace.calls[1].enter)[0]
        self.assertEqual(len(raw), 8)

    def test_dynamic_snapshot_requires_enter(self):
        payload = b"x" * 8
        registers = (10, 11, 2, 0, 0)
        trace = decode_trace(encode_trace(call_events(payload, flags=1, registers=registers)))
        definitions = [{"phase": "enter", "source": "arg1", "size_arg": 2,
                        "multiplier": 4, "max": 256}]
        with self.assertRaises(TraceError):
            snapshot_parts(trace.calls[1].enter, definitions, enter=None)

    def test_failed_optional_snapshot_is_none(self):
        trace = decode_trace(encode_trace(call_events(b"\x00" * 8, flags=0)))
        definitions = [{"phase": "enter", "source": "arg0", "size": 8, "optional": True}]
        _, raw = snapshot_parts(trace.calls[1].enter, definitions)[0]
        self.assertIsNone(raw)

    def test_missing_required_snapshot_raises(self):
        trace = decode_trace(encode_trace(call_events(b"\x00" * 8, flags=0)))
        definitions = [{"phase": "enter", "source": "arg0", "size": 8}]
        with self.assertRaises(TraceError):
            snapshot_parts(trace.calls[1].enter, definitions)


if __name__ == "__main__":
    unittest.main()
