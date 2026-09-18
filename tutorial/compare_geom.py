#!/usr/bin/env python3
"""Adapter: compare a reimplementation of the geom library against a recording.

    python3 tutorial/compare_geom.py build/demo.bin \\
        [--serial <serial> --compiler <ndk-clang++>]

Steps, in the order the design requires:

1.  Validate the recording independently (probe set, module identity, call
    shape, argument ABI, snapshot sizes). This layer is checked against the
    *official* evidence and must not be relaxed to make an implementation pass.
2.  Encode the validated recording into the replay program's input stream.
3.  Run the replay program. It executes ``mine.cpp`` and writes its results.
4.  Compare the reimplementation's output to the recorded output byte for byte.

The recording is the oracle. Nothing here reads ``official.cpp``.
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

KIND_SCALE = 100
KIND_DIRECTION = 200

# Replay a bounded prefix of the recording. A device recording holds thousands
# of calls; the adapter does not need all of them to prove equivalence, and a
# small deterministic set keeps the comparison readable.
SCALE_CASES = 32
DIRECTION_CASES = 32

# The library under test is pinned by the probe set. The adapter repeats the
# check so a trace from another build cannot be replayed by accident.
PROBE_SET_NAME = "probe-set"


def _snapshot_map(call, phase: str) -> dict[str, bytes]:
    """Return source/register -> bytes for one phase, failing on a missing capture."""
    probe = call_kind_probe[call.enter.kind]
    parts = snapshot_parts(call.enter if phase == "enter" else call.leave,
                           probe["snapshots"])
    result: dict[str, bytes] = {}
    for definition, raw in parts:
        if definition["phase"] != phase:
            continue
        key = definition.get("register") or definition["source"]
        require(raw is not None, f"snapshot {key} was not captured in {phase}")
        result[key] = raw
    return result


call_kind_probe: dict[int, dict] = {}


def _bind_probes(trace) -> None:
    call_kind_probe.clear()
    for probe in trace.module["probes"]:
        call_kind_probe[probe["kind"]] = probe


def _scale_case(trace, call) -> tuple[bytes, bytes]:
    """Validate one geom_scale call.

    ABI: ``float a`` -> s0, ``float b`` -> s1, ``float* out`` -> arg0.
    The leave snapshot re-captures s0, which holds the returned float.
    """
    enter = _snapshot_map(call, "enter")
    leave = _snapshot_map(call, "leave")
    a = struct.unpack("<f", enter["s0"][:4])[0]
    b = struct.unpack("<f", enter["s1"][:4])[0]
    return_value = struct.unpack("<f", leave["s0"][:4])[0]
    out0, out1 = struct.unpack("<ff", leave["arg0"][:8])
    return struct.pack("<ff", a, b), struct.pack("<fff", out0, out1, return_value)


def _direction_case(trace, call) -> tuple[bytes, bytes]:
    """Validate one geom_direction call.

    ABI: ``float x,y,z`` -> s0,s1,s2, ``float* out`` -> arg0.
    """
    enter = _snapshot_map(call, "enter")
    leave = _snapshot_map(call, "leave")
    xyz = struct.unpack("<fff", enter["s0"][:4] + enter["s1"][:4] + enter["s2"][:4])
    out = struct.unpack("<fff", leave["arg0"][:12])
    return struct.pack("<fff", *xyz), struct.pack("<fff", *out)


def encode(trace) -> tuple[bytes, bytes, list[str]]:
    """Validate the recording, then emit (replay_input, expected, case_names)."""
    _bind_probes(trace)
    scale_calls = [c for c in trace.calls.values() if c.kind == KIND_SCALE]
    direction_calls = [c for c in trace.calls.values() if c.kind == KIND_DIRECTION]
    require(scale_calls and direction_calls, "recording is missing one of the two probes")

    # Every call must be complete and have no recorded children: this fixture is
    # a synchronous leaf, so a child or an unterminated call means a bad capture.
    for call in (*scale_calls, *direction_calls):
        require(call.complete, f"call {call.kind} has no leave record")
        require(not call.children, f"call {call.kind} unexpectedly has children")

    scale = scale_calls[:SCALE_CASES]
    direction = direction_calls[:DIRECTION_CASES]
    names, inputs, expected = [], bytearray(), bytearray()
    inputs += struct.pack("<II", len(scale), len(direction))
    for index, call in enumerate(scale):
        case_input, case_expected = _scale_case(trace, call)
        inputs += case_input
        expected += case_expected
        names.append(f"scale[{index}]")
    for index, call in enumerate(direction):
        case_input, case_expected = _direction_case(trace, call)
        inputs += case_input
        expected += case_expected
        names.append(f"direction[{index}]")
    return bytes(inputs), bytes(expected), names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    add_replay_arguments(parser)
    args = parser.parse_args(argv)

    trace = load(args.trace, PROBE_SET_NAME)
    inputs, expected, names = encode(trace)
    include = ROOT / "tutorial" / "example"
    # Link the reimplementation. The adapter never links the official library;
    # `mine.cpp` is the only definition of the two symbols.
    replay = run_replay(ROOT / "tutorial" / "replay_geom.cpp", inputs, [include],
                        serial=args.serial, compiler=args.compiler,
                        extra_sources=(include / "mine.cpp",))
    require(len(replay.output) == len(expected),
            f"replay returned {len(replay.output)} bytes, expected {len(expected)}")
    for index, (actual, want) in enumerate(zip(replay.output, expected)):
        require(actual == want,
                f"{names[index // 12]} byte {index % 12}: "
                f"actual {actual:#04x} != official {want:#04x}")
    print(json.dumps({
        "result": "PASS",
        "cases": len(names),
        "bytes": len(expected),
        "session_id": trace.metadata.get("session_id"),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "native": replay.identity,
        "scope": "geom_scale and geom_direction with the recorded argument values; "
                 "the recording covers only the inputs the driver used",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
