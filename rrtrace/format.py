"""Binary trace format for recorded call contracts.

This module is deliberately dependency-free (standard library only) and does
not import Frida or touch a device. It is the single definition of the file
format, used by both the recorder and the replayer.

Layout
------

    file header      fixed 64 bytes
    metadata         UTF-8 JSON, padded with NUL to an 8-byte boundary
    event stream     concatenated event records, each padded to 8 bytes

Every integer is little-endian.

Event record header (40 bytes):

    offset  size  field
    0       4     total_size      record length including padding
    4       2     header_size     always 40 in this version
    6       2     kind            probe-defined integer id
    8       1     phase           1 enter, 2 leave, 3 instruction hit
    9       1     flags           bit i set when snapshot i was captured
    10      2     payload_size    bytes after the header, before padding
    12      4     tid             native thread id
    16      4     sequence        per-thread monotonic counter
    20      8     timestamp_ns    CLOCK_MONOTONIC at record time
    28      8     correlation     enter/leave pairing id (0 for instruction)
    36      4     reserved
    40      40    registers       five u64 words (arg0..arg3, lr or retval)
    80      ...   snapshot        concatenated probe snapshots

A *call* is an enter record and the leave record that shares its correlation
id on the same thread. A leave without an enter, or a leftover enter, is a
recording defect and `Trace.match_calls` reports it rather than hiding it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import struct

FILE_MAGIC = b"ARFRIDA1"
FILE_HEADER = struct.Struct("<8sHHHHBBHIQQQQ8s")
EVENT_HEADER = struct.Struct("<IHHBBHIIQQI")
EVENT_HEADER_SIZE = EVENT_HEADER.size

PHASES = {1: "enter", 2: "leave", 3: "instruction"}


class TraceError(ValueError):
    """A recording is malformed or violates its own declared metadata."""


def require(condition, message: str) -> None:
    if not condition:
        raise TraceError(message)


@dataclass(frozen=True)
class Event:
    kind: int
    phase: int
    flags: int
    tid: int
    sequence: int
    timestamp_ns: int
    correlation: int
    registers: tuple[int, ...]
    snapshot: bytes

    @property
    def phase_name(self) -> str:
        return PHASES.get(self.phase, f"phase{self.phase}")

    def as_dict(self) -> dict:
        return dict(kind=self.kind, phase=self.phase, flags=self.flags, tid=self.tid,
                    sequence=self.sequence, timestamp_ns=self.timestamp_ns,
                    correlation=self.correlation, registers=self.registers,
                    snapshot=self.snapshot)


@dataclass
class Call:
    """One enter/leave pair on one thread, with its recorded child calls."""

    enter: Event
    leave: Event | None = None
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    instructions: list[Event] = field(default_factory=list)

    @property
    def kind(self) -> int:
        return self.enter.kind

    @property
    def complete(self) -> bool:
        return self.leave is not None


@dataclass
class Trace:
    metadata: dict
    events: list[Event]
    calls: dict[int, Call]
    roots: dict[int, list[int]]
    unscoped_instructions: list[Event]

    @property
    def probe_set(self) -> dict:
        return self.metadata["probe_set"]

    @property
    def module(self) -> dict:
        return self.metadata["agent_summary"]["module"]


def decode_trace(data: bytes) -> Trace:
    """Parse an in-memory trace. Raises TraceError on any framing defect."""
    require(len(data) >= FILE_HEADER.size, "file shorter than its header")
    (magic, header_size, schema, _reserved, _h2, _h3, _flags, _pad,
     metadata_size, _start, _end, _q1, _q2, _tail) = FILE_HEADER.unpack_from(data, 0)
    require(magic == FILE_MAGIC, f"bad magic {magic!r}")
    require(header_size == FILE_HEADER.size, f"unexpected header size {header_size}")
    require(schema == 1, f"unsupported schema {schema}")

    metadata_start = FILE_HEADER.size
    metadata_end = metadata_start + metadata_size
    require(metadata_end <= len(data), "metadata extends past end of file")
    metadata = json.loads(data[metadata_start:metadata_end].decode("utf-8"))

    events = _decode_events(data, (metadata_end + 7) & ~7)
    return _assemble(metadata, events)


def read_trace(path: Path) -> Trace:
    return decode_trace(Path(path).read_bytes())


def _decode_events(data: bytes, offset: int) -> list[Event]:
    events: list[Event] = []
    while offset + EVENT_HEADER_SIZE <= len(data):
        (total_size, header_size, kind, phase, flags, payload_size,
         tid, sequence, timestamp_ns, correlation, _reserved) = EVENT_HEADER.unpack_from(data, offset)
        require(header_size == EVENT_HEADER_SIZE, f"event header size {header_size}")
        require(total_size >= EVENT_HEADER_SIZE, f"event total size {total_size}")
        require(offset + total_size <= len(data), "event extends past end of file")
        require(phase in PHASES, f"unknown phase {phase}")
        payload = data[offset + EVENT_HEADER_SIZE: offset + EVENT_HEADER_SIZE + payload_size]
        registers = struct.unpack_from("<5Q", payload, 0)
        snapshot = payload[40:]
        events.append(Event(kind=kind, phase=phase, flags=flags, tid=tid,
                            sequence=sequence, timestamp_ns=timestamp_ns,
                            correlation=correlation, registers=registers,
                            snapshot=snapshot))
        offset += total_size
    return events


def _assemble(metadata: dict, events: list[Event]) -> Trace:
    """Pair enters with leaves and rebuild the per-thread call forest."""
    calls: dict[int, Call] = {}
    roots: dict[int, list[int]] = {}
    stacks: dict[int, list[int]] = {}
    unscoped: list[Event] = []

    for event in events:
        if event.phase == 3:
            stack = stacks.get(event.tid, [])
            if stack:
                calls[stack[-1]].instructions.append(event)
            else:
                unscoped.append(event)
            continue

        if event.phase == 1:
            require(event.correlation not in calls,
                    f"duplicate correlation id {event.correlation}")
            stack = stacks.setdefault(event.tid, [])
            call = Call(enter=event, parent=stack[-1] if stack else None)
            calls[event.correlation] = call
            stack.append(event.correlation)
            if call.parent is None:
                roots.setdefault(event.tid, []).append(event.correlation)
            else:
                calls[call.parent].children.append(event.correlation)
            continue

        stack = stacks.get(event.tid, [])
        require(stack and stack[-1] == event.correlation,
                "leave without matching enter")
        calls[event.correlation].leave = event
        stack.pop()

    for tid, stack in stacks.items():
        require(not stack, f"thread {tid:#x} has unterminated calls")
    return Trace(metadata=metadata, events=events, calls=calls, roots=roots,
                 unscoped_instructions=unscoped)


def expect_children(trace: Trace, call: Call, kinds: list[int]) -> list[Call]:
    """Assert that a call's recorded children are exactly `kinds`, in order."""
    children = [trace.calls[i] for i in call.children]
    require([c.kind for c in children] == kinds,
            f"call {call.kind} children {[c.kind for c in children]} != {kinds}")
    return children


def snapshot_parts(event: Event, definitions: list[dict]) -> list[tuple[dict, bytes | None]]:
    """Decode an event's snapshot using the probe definitions from its metadata.

    Returns (definition, bytes) pairs. `None` marks a failed optional capture;
    zero-filled failed reads must never be interpreted as observed memory.
    """
    phase = PHASES[event.phase]
    selected = [d for d in definitions if d["phase"] == phase]
    require(len(selected) <= 8, "more than eight snapshots in one phase")
    require(event.flags >> len(selected) == 0, "snapshot flags exceed definitions")
    result: list[tuple[dict, bytes | None]] = []
    cursor = 0
    for index, definition in enumerate(selected):
        # A register snapshot is always eight bytes (one u64) and needs no size
        # or source field; a memory snapshot declares its own size.
        size = 8 if "register" in definition else definition.get("size", 0)
        require(isinstance(size, int) and size >= 0, "invalid snapshot size")
        require(cursor + size <= len(event.snapshot), "snapshot descriptor overruns payload")
        raw = event.snapshot[cursor:cursor + size]
        valid = bool(event.flags & (1 << index))
        require(valid or definition.get("optional", False), "missing required snapshot")
        result.append((definition, raw if valid else None))
        cursor += size
    return result
