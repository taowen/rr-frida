"""Write a trace file from agent batches.

The writer validates every batch before it is appended, so a torn or
out-of-order stream cannot produce a file that merely *looks* well formed.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct

from rrtrace.format import EVENT_HEADER, EVENT_HEADER_SIZE, FILE_HEADER, FILE_MAGIC


class WriteError(ValueError):
    """A batch is malformed or arrives out of order."""


class TraceWriter:
    """Append batches to an in-memory event stream, then emit the trace file."""

    def __init__(self, batch_bytes: int = 64 * 1024) -> None:
        self._batch_bytes = batch_bytes
        self._events = bytearray()
        self._next_batch = 1
        self._records = 0
        self.installed_status: dict | None = None
        self.install_error: str | None = None
        self.error: str | None = None
        self.waiting_module: str | None = None
        self.summary: dict | None = None

    @property
    def records(self) -> int:
        return self._records

    def on_message(self, message: dict, data: bytes | None) -> None:
        # Frida delivers its own envelopes (for example {"type": "error"})
        # alongside our payloads. Only a string "send" payload is ours; anything
        # else must be surfaced, not silently dropped, because a recording made
        # after a hidden agent error is worse than no recording.
        if message.get("type") != "send":
            raise WriteError(f"frida message is not a payload: {message.get('type')!r} "
                             f"{message.get('description') or ''}".strip())
        payload = message.get("payload")
        if not isinstance(payload, dict) or "type" not in payload:
            raise WriteError(f"agent payload missing a type: {payload!r}")
        kind = payload["type"]
        if kind == "installed":
            self.installed_status = payload["status"]
        elif kind == "install_error":
            self.install_error = payload["error"]
        elif kind == "agent_error":
            self.error = payload["error"]
        elif kind == "batch":
            self._append_batch(payload, data)
        elif kind == "waiting":
            self.waiting_module = payload.get("module")
        else:
            raise WriteError(f"unexpected agent message: {kind}")

    def _append_batch(self, message: dict, data: bytes | None) -> None:
        if data is None:
            raise WriteError("batch arrived without a payload")
        sequence = message.get("batch_sequence")
        if sequence != self._next_batch:
            raise WriteError(f"batch {sequence} out of order, expected {self._next_batch}")
        self._next_batch += 1
        if len(data) != message.get("bytes"):
            raise WriteError("batch length does not match its declared size")
        records = message.get("records")
        if not isinstance(records, int) or records <= 0:
            raise WriteError("batch record count must be positive")
        offset = 0
        seen = 0
        while offset < len(data):
            if offset + EVENT_HEADER_SIZE > len(data):
                raise WriteError("event header crosses the batch boundary")
            (total_size, header_size) = struct.unpack_from("<IH", data, offset)
            if header_size != EVENT_HEADER_SIZE or total_size < EVENT_HEADER_SIZE:
                raise WriteError("invalid event header inside batch")
            if offset + total_size > len(data):
                raise WriteError("event crosses the batch boundary")
            self._events += data[offset:offset + total_size]
            offset += total_size
            seen += 1
        if seen != records:
            raise WriteError(f"batch declared {records} records but carried {seen}")
        self._records += seen

    def finish(self, path: Path, metadata: dict) -> dict:
        """Write the file and return a manifest for the caller."""
        metadata_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
        padding = (-len(metadata_bytes)) % 8
        header = FILE_HEADER.pack(FILE_MAGIC, FILE_HEADER.size, 1, 0, 0, 0, 0, 0,
                                  len(metadata_bytes), 0, 0, 0, 0, b"\0" * 8)
        Path(path).write_bytes(header + metadata_bytes + b"\0" * padding + bytes(self._events))
        return {"records": self._records, "bytes": len(self._events)}
