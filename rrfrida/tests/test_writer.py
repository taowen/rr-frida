"""Tests for recorder-side integrity checks.

    python3 -m unittest rrfrida.tests.test_writer
"""

from __future__ import annotations

import struct
import unittest

from rrfrida.trace_writer import TraceWriter, WriteError
from rrtrace.format import EVENT_HEADER, EVENT_HEADER_SIZE


def encode_event(kind, phase, tid, sequence, correlation, registers=(0, 0, 0, 0, 0)):
    payload_size = 40
    total_size = (EVENT_HEADER_SIZE + payload_size + 7) & ~7
    body = bytearray(total_size)
    EVENT_HEADER.pack_into(body, 0, total_size, EVENT_HEADER_SIZE, kind, phase, 0,
                           payload_size, tid, sequence, 1, correlation, 0)
    struct.pack_into("<5Q", body, EVENT_HEADER_SIZE, *registers)
    return bytes(body)


def writer_with(events) -> TraceWriter:
    writer = TraceWriter()
    data = b"".join(events)
    writer.on_message({"type": "send", "payload": {
        "type": "batch", "batch_sequence": 1, "bytes": len(data),
        "records": len(events)}}, data)
    return writer


class TestWriter(unittest.TestCase):
    def test_balanced_calls_accepted(self):
        writer = writer_with([
            encode_event(100, 1, 0x10, 1, 1),
            encode_event(100, 2, 0x10, 2, 1),
        ])
        writer.finish.__self__  # noqa: B018 - keep the object alive
        counts = writer.required_probe_summary()
        self.assertEqual(counts["100"]["enter"], 1)
        self.assertEqual(counts["100"]["leave"], 1)

    def test_sequence_gap_rejected(self):
        # Thread 0x10 sends sequences 1 then 3; sequence 2 is missing.
        writer = writer_with([
            encode_event(100, 1, 0x10, 1, 1),
            encode_event(100, 2, 0x10, 3, 1),
        ])
        with self.assertRaises(WriteError):
            writer.validate_sequence()

    def test_unbalanced_leave_detected(self):
        writer = writer_with([encode_event(100, 2, 0x10, 1, 9)])
        self.assertEqual(writer._unbalanced_correlations(), 1)

    def test_batch_out_of_order_rejected(self):
        writer = TraceWriter()
        data = encode_event(100, 1, 0x10, 1, 1)
        with self.assertRaises(WriteError):
            writer.on_message({"type": "send", "payload": {
                "type": "batch", "batch_sequence": 2, "bytes": len(data),
                "records": 1}}, data)

    def test_batch_length_mismatch_rejected(self):
        writer = TraceWriter()
        with self.assertRaises(WriteError):
            writer.on_message({"type": "send", "payload": {
                "type": "batch", "batch_sequence": 1, "bytes": 9999,
                "records": 1}}, b"\x00" * 48)

    def test_non_payload_message_rejected(self):
        writer = TraceWriter()
        with self.assertRaises(WriteError):
            writer.on_message({"type": "error", "description": "boom"}, None)


if __name__ == "__main__":
    unittest.main()
