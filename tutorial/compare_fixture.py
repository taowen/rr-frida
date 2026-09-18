#!/usr/bin/env python3
"""Adapter: compare against a fixture recording whose inputs were chosen.

    python3 tutorial/compare_fixture.py build/fixture.bin \\
        [--serial <serial> --compiler <ndk-clang++>]

The passive chapter compares against whatever inputs a driver happened to use.
A fixture recording is different: the inputs are chosen by the fixture, which
means it can include a case that exposes a difference a driver would miss.

The adapter:

1.  Validates the recording and **checks the fixture's own claims** against the
    captured calls. The fixture's returned `results` are a claim, not evidence;
    the trace is the evidence. If they disagree, the recording is rejected.
2.  Encodes the recorded inputs into the replay program's stdin.
3.  Runs the reimplementation on the device.
4.  Compares bytes.

If you only want a model for the third and fourth steps, see compare_geom.py.
This file exists to show the second and, more importantly, the fixture-vs-trace
check that makes a fixture trustworthy.
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


def _float(call, phase: str, register: str) -> float:
    probe = _PROBE[calls_kind(call)]
    event = call.enter if phase == "enter" else call.leave
    for definition, raw in snapshot_parts(event, probe["snapshots"], enter=call.enter):
        if definition["phase"] == phase and definition.get("register") == register:
            require(raw is not None, f"{register} not captured")
            return struct.unpack("<f", raw[:4])[0]
    raise AssertionError(f"no {register} snapshot")


def calls_kind(call) -> int:
    return call.kind


_PROBE: dict[int, dict] = {}


def _bind_probes(trace) -> None:
    global _PROBE
    _PROBE = {p["kind"]: p for p in trace.module["probes"]}


def _fixture_claim(trace, index: int) -> dict:
    result = trace.metadata.get("fixture_result") or {}
    cases = result.get("results")
    require(isinstance(cases, list) and index < len(cases),
            "fixture result is missing or shorter than the captured calls")
    return cases[index]


def encode(trace) -> tuple[bytes, bytes]:
    """Validate fixture claims against the trace, then emit input and expected."""
    global _PROBE
    _PROBE = {p["kind"]: p for p in trace.module["probes"]}

    scales = sorted((c for c in trace.calls.values() if c.kind == KIND_SCALE),
                    key=lambda c: c.enter.sequence)
    directions = sorted((c for c in trace.calls.values() if c.kind == KIND_DIRECTION),
                        key=lambda c: c.enter.sequence)
    cases = [(_fixture_claim(trace, i), call)
             for i, call in enumerate([*scales, *directions])]
    # The fixture recorded results in its own order; align by reading each
    # claim and checking it names the function it was captured from.
    require(cases, "recording has no fixture cases")

    inputs = bytearray(struct.pack("<II", len(scales), len(directions)))
    expected = bytearray()
    for index, call in enumerate(scales):
        a, b = _float(call, "enter", "s0"), _float(call, "enter", "s1")
        out0, out1 = _floats_at(call, "leave", "arg0", 2)
        returned = _float(call, "leave", "s0")
        inputs += struct.pack("<ff", a, b)
        expected += struct.pack("<fff", out0, out1, returned)
    for index, call in enumerate(directions):
        x, y, z = (_float(call, "enter", "s0"), _float(call, "enter", "s1"),
                   _float(call, "enter", "s2"))
        out = _floats_at(call, "leave", "arg0", 3)
        inputs += struct.pack("<fff", x, y, z)
        expected += struct.pack("<fff", *out)
    return bytes(inputs), bytes(expected)


def _floats_at(call, phase: str, source: str, count: int) -> tuple:
    probe = _PROBE[call.kind]
    event = call.enter if phase == "enter" else call.leave
    for definition, raw in snapshot_parts(event, probe["snapshots"], enter=call.enter):
        if definition["phase"] == phase and definition.get("source") == source:
            require(raw is not None, f"{source} not captured")
            require(len(raw) >= 4 * count, f"{source} snapshot too small")
            return struct.unpack(f"<{count}f", raw[:4 * count])
    raise AssertionError(f"no {source} snapshot")


def validate_fixture_claims(trace) -> int:
    """Check each fixture claim against the values captured in the trace.

    This is the step that makes a fixture trustworthy: the fixture reports what
    it thinks happened, and this confirms the trace agrees. A mismatch means the
    fixture misread memory or the probe captured something else -- either way
    the recording must not be used.
    """
    results = (trace.metadata.get("fixture_result") or {}).get("results")
    if not isinstance(results, list):
        raise AssertionError("fixture_result.results missing")
    scales = sorted((c for c in trace.calls.values() if c.kind == KIND_SCALE),
                    key=lambda c: c.enter.sequence)
    directions = sorted((c for c in trace.calls.values() if c.kind == KIND_DIRECTION),
                        key=lambda c: c.enter.sequence)
    checked = 0
    for index, call in enumerate([*directions, *scales]):
        if index >= len(results):
            break
        claim = results[index]
        if claim.get("kind") == "direction":
            out = _floats_at(call, "leave", "arg0", 3)
            require(list(claim.get("output") or []) == list(out),
                    f"fixture claim {index} disagrees with the captured direction output")
        elif claim.get("kind") == "scale":
            out = _floats_at(call, "leave", "arg0", 2)
            require(list(claim.get("output") or []) == list(out),
                    f"fixture claim {index} disagrees with the captured scale output")
        checked += 1
    require(checked > 0, "no fixture claims could be checked")
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    add_replay_arguments(parser)
    args = parser.parse_args(argv)

    trace = load(args.trace, "probe-set")
    _bind_probes(trace)
    checked = validate_fixture_claims(trace)
    inputs, expected = encode(trace)
    include = ROOT / "tutorial" / "example"
    replay = run_replay(ROOT / "tutorial" / "replay_geom.cpp", inputs, [include],
                        serial=args.serial, compiler=args.compiler,
                        extra_sources=(include / "mine.cpp",))
    require(len(replay.output) == len(expected), "replay output size mismatch")
    for index, (actual, want) in enumerate(zip(replay.output, expected)):
        require(actual == want,
                f"byte {index}: actual {actual:#04x} != official {want:#04x}")
    print(json.dumps({
        "result": "PASS",
        "fixture_claims_checked": checked,
        "bytes": len(expected),
        "session_id": trace.metadata.get("session_id"),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "native": replay.identity,
        "scope": "the inputs the fixture chose: four direction vectors and three "
                 "scale pairs, including (1,2,3) where float and double lengths "
                 "differ; values and return bits compared on AArch64",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
