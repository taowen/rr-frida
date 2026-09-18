#!/usr/bin/env python3
"""Adapter: compare a time-dependent function using a logical clock.

    python3 tutorial/compare_predict.py build/predict.bin \\
        [--serial <serial> --compiler <ndk-clang++>]

A function that reads CLOCK_MONOTONIC cannot be replayed against a recording
unless the clock readings are part of the recording. This adapter shows the
three steps that makes it work:

1.  Read the recorded clock readings from the trace.
2.  Turn them into the elapsed values the function observed, applying the same
    first-call rule the original used.
3.  Feed those values to the reimplementation and compare.

Without step 2 the reimplementation would read the real clock and produce a
different number every run -- a comparison that can never pass, or worse, one
that passes by coincidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rrfrida.native_replay import add_replay_arguments, run_replay  # noqa: E402
from rrfrida.validate import load  # noqa: E402
from rrtrace.format import require, snapshot_parts  # noqa: E402

KIND_PREDICT = 340


def _probe_map(trace) -> dict[int, dict]:
    return {probe["kind"]: probe for probe in trace.module["probes"]}


def _reading_ns(reading: dict) -> int:
    return reading["seconds"] * 1_000_000_000 + reading["nanos"]


def _f32(value: float) -> float:
    """Round a Python double to the nearest float32, the way C would."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def elapsed_values(trace) -> list[float]:
    """Turn recorded clock readings into the elapsed value the function saw.

    The function reads the clock once per call and computes the difference from
    the previous call:

        call 0: no previous reading -> elapsed 0 (it establishes the origin)
        call i: reading[i] - reading[i-1]

    **The arithmetic is part of the contract.** The official code narrows the
    nanosecond difference to `float` *before* dividing and multiplying, so the
    elapsed value must be computed the same way. Computing it in double and
    narrowing at the end is off by one ULP on some intervals -- exactly the
    failure mode this project exists to catch, now appearing in the adapter
    itself. Keep the float32 rounding at each step.
    """
    readings = trace.metadata.get("clock_readings") or []
    require(len(readings) >= 1,
            "the recording has no clock readings; record with --capture-clock")
    values = [0.0]
    for index in range(1, len(readings)):
        difference_ns = _reading_ns(readings[index]) - _reading_ns(readings[index - 1])
        # Mirror the C: narrow to float, divide by one million as a float, then
        # the caller multiplies by the magnitude.
        values.append(_f32(_f32(float(difference_ns)) / _f32(1000000.0)))
    return values


def _float_at(call, phase: str, register: str) -> float:
    probe = {p["kind"]: p for p in call_probes}[call.kind]
    event = call.enter if phase == "enter" else call.leave
    for definition, raw in snapshot_parts(event, probe["snapshots"], enter=call.enter):
        if definition["phase"] == phase and definition.get("register") == register:
            require(raw is not None, f"{register} not captured")
            return struct.unpack("<f", raw[:4])[0]
    raise AssertionError(f"no {register} snapshot")


call_probes: list = []


def expected_bytes(trace) -> bytes:
    """The recorded outputs, in the format the replay writes."""
    out = bytearray()
    for case in (trace.metadata.get("fixture_result") or {}).get("results", []):
        out += struct.pack("<fff", case["delta"], case["magnitude"], case["returned"])
    return bytes(out)


def encode(trace) -> tuple[bytes, bytes]:
    """Merge the recorded inputs with the recorded elapsed values."""
    calls = sorted((c for c in trace.calls.values() if c.kind == KIND_PREDICT),
                   key=lambda c: c.enter.sequence)
    gaps = elapsed_values(trace)
    require(len(gaps) == len(calls),
            f"{len(calls)} predict calls but {len(gaps)} clock intervals; "
            "record with --capture-clock so every call has a reading")
    probe = _probe_map(trace)[KIND_PREDICT]

    inputs = bytearray(struct.pack("<I", len(calls)))
    for call, elapsed in zip(calls, gaps):
        count = call.enter.registers[1] & 0xFFFFFFFF
        samples = None
        for definition, raw in snapshot_parts(call.enter, probe["snapshots"],
                                              enter=call.enter):
            if definition.get("source") == "arg0" and raw is not None:
                samples = raw
        require(samples is not None and len(samples) >= 4 * count,
                "recorded input buffer is missing or too small")
        inputs += struct.pack("<i", count)
        inputs += bytes(samples[:4 * count])
        inputs += struct.pack("<f", elapsed)
    return bytes(inputs), expected_bytes(trace)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    add_replay_arguments(parser)
    args = parser.parse_args(argv)

    global call_probes
    trace = load(args.trace, "predict-probe-set")
    call_probes = trace.module["probes"]

    gaps = elapsed_values(trace)
    inputs, expected = encode(trace)
    include = ROOT / "tutorial" / "example"
    replay = run_replay(ROOT / "tutorial" / "replay_predict.cpp", inputs, [include],
                        serial=args.serial, compiler=args.compiler,
                        extra_sources=(include / "pipeline_mine_predict.cpp",))
    require(len(replay.output) == len(expected),
            f"replay returned {len(replay.output)} bytes, expected {len(expected)}")
    for index, (actual, want) in enumerate(zip(replay.output, expected)):
        require(actual == want,
                f"byte {index} of case {index // 12}: "
                f"actual {actual:#04x} != official {want:#04x}")
    print(json.dumps({
        "result": "PASS",
        "cases": len(gaps),
        "elapsed_ms": [round(g, 6) for g in gaps],
        "bytes": len(expected),
        "session_id": trace.metadata.get("session_id"),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "native": replay.identity,
        "scope": "pipeline_predict with the recorded CLOCK_MONOTONIC intervals; "
                 "the reimplementation reads no clock of its own, so the result "
                 "is reproducible. The origin rule and the first call's elapsed "
                 "value are taken from the recording",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
