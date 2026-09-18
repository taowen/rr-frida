"""The single definition of the trace format shared by recorder and replayer."""

from rrtrace.format import (
    EVENT_HEADER,
    EVENT_HEADER_SIZE,
    FILE_HEADER,
    FILE_MAGIC,
    PHASES,
    Call,
    Event,
    Trace,
    TraceError,
    decode_trace,
    expect_children,
    read_trace,
    require,
    snapshot_parts,
)

__all__ = [
    "EVENT_HEADER", "EVENT_HEADER_SIZE", "FILE_HEADER", "FILE_MAGIC", "PHASES",
    "Call", "Event", "Trace", "TraceError", "decode_trace", "expect_children",
    "read_trace", "require", "snapshot_parts",
]
