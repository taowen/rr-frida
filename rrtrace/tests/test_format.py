"""Round-trip and defect tests for the trace format. No device or Frida needed.

    python3 -m unittest rrtrace.tests.test_format
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
    TraceError,
    decode_trace,
    expect_children,
    snapshot_parts,
)


def encode_event(kind, phase, tid, sequence, correlation, registers, snapshot=b"",
                 flags=0, timestamp_ns=0, total_override=None):
    payload_size = 40 + len(snapshot)
    total_size = (EVENT_HEADER_SIZE + payload_size + 7) & ~7
    body = bytearray(total_size)
    EVENT_HEADER.pack_into(body, 0, total_size, EVENT_HEADER_SIZE, kind, phase, flags,
                           payload_size, tid, sequence, timestamp_ns, correlation, 0)
    struct.pack_into("<5Q", body, EVENT_HEADER_SIZE, *registers)
    body[EVENT_HEADER_SIZE + 40:EVENT_HEADER_SIZE + 40 + len(snapshot)] = snapshot
    if total_override is not None:
        struct.pack_into("<I", body, 0, total_override)
    return bytes(body)


def encode_trace(metadata, events):
    metadata_bytes = json.dumps(metadata).encode("utf-8")
    padding = (-len(metadata_bytes)) % 8
    header = FILE_HEADER.pack(FILE_MAGIC, FILE_HEADER.size, 1, 0, 0, 0, 0, 0,
                              len(metadata_bytes), 0, 0, 0, 0, b"\0" * 8)
    return header + metadata_bytes + b"\0" * padding + b"".join(events)


METADATA = {
    "schema": 1,
    "session_id": "test",
    "probe_set": {"name": "demo", "required": ["add"], "module": "libdemo.so"},
    "agent_summary": {"module": {"name": "libdemo.so", "base": "0x1000",
                                 "size": 0x1000, "build_id": "abc",
                                 "probes": [{"name": "add", "kind": 100, "type": "function",
                                             "rva": "0x10", "required": True,
                                             "snapshots": [{"phase": "enter", "source": "arg0",
                                                            "size": 8}]}]}},
}


class TestFormat(unittest.TestCase):
    def test_round_trip_single_call(self):
        events = [
            encode_event(100, 1, 0x10, 1, 7, (1, 2, 0, 0, 0), b"\x01" * 8),
            encode_event(100, 2, 0x10, 2, 7, (3, 0, 0, 0, 0), b"\x03" * 8),
        ]
        trace = decode_trace(encode_trace(METADATA, events))
        self.assertEqual(len(trace.calls), 1)
        call = trace.calls[7]
        self.assertTrue(call.complete)
        self.assertEqual(call.enter.registers[0], 1)
        self.assertEqual(call.leave.registers[0], 3)
        self.assertEqual(trace.roots[0x10], [7])

    def test_nested_children(self):
        events = [
            encode_event(100, 1, 0x10, 1, 1, (0, 0, 0, 0, 0)),
            encode_event(200, 1, 0x10, 2, 2, (0, 0, 0, 0, 0)),
            encode_event(200, 2, 0x10, 3, 2, (0, 0, 0, 0, 0)),
            encode_event(100, 2, 0x10, 4, 1, (0, 0, 0, 0, 0)),
        ]
        trace = decode_trace(encode_trace(METADATA, events))
        parent = trace.calls[1]
        children = expect_children(trace, parent, [200])
        self.assertEqual(children[0].parent, 1)
        self.assertEqual(trace.roots[0x10], [1])

    def test_instruction_attaches_to_open_call(self):
        events = [
            encode_event(100, 1, 0x10, 1, 1, (0, 0, 0, 0, 0)),
            encode_event(300, 3, 0x10, 2, 0, (0, 0, 0, 0, 0)),
            encode_event(100, 2, 0x10, 3, 1, (0, 0, 0, 0, 0)),
        ]
        trace = decode_trace(encode_trace(METADATA, events))
        self.assertEqual(len(trace.calls[1].instructions), 1)
        self.assertEqual(trace.unscoped_instructions, [])

    def test_snapshot_parts_marks_failed_optional(self):
        definition = [{"phase": "enter", "size": 8, "optional": True}]
        event = decode_trace(encode_trace(METADATA, [
            encode_event(100, 1, 0x10, 1, 1, (0, 0, 0, 0, 0), b"\x00" * 8, flags=0),
            encode_event(100, 2, 0x10, 2, 1, (0, 0, 0, 0, 0)),
        ])).calls[1].enter
        parts = snapshot_parts(event, definition)
        self.assertIsNone(parts[0][1])

    def test_bad_magic_rejected(self):
        data = bytearray(encode_trace(METADATA, []))
        data[0:8] = b"BADMAGIC"
        with self.assertRaises(TraceError):
            decode_trace(bytes(data))

    def test_unmatched_leave_rejected(self):
        events = [encode_event(100, 2, 0x10, 1, 9, (0, 0, 0, 0, 0))]
        with self.assertRaises(TraceError):
            decode_trace(encode_trace(METADATA, events))

    def test_unterminated_call_rejected(self):
        events = [encode_event(100, 1, 0x10, 1, 9, (0, 0, 0, 0, 0))]
        with self.assertRaises(TraceError):
            decode_trace(encode_trace(METADATA, events))

    def test_event_overrun_rejected(self):
        events = [encode_event(100, 1, 0x10, 1, 9, (0, 0, 0, 0, 0), total_override=0xffff)]
        with self.assertRaises(TraceError):
            decode_trace(encode_trace(METADATA, events))


if __name__ == "__main__":
    unittest.main()
