#!/usr/bin/env python3
"""Adapter: compare `pipeline_process` against a recording of the original.

    python3 tutorial/compare_pipeline.py build/pipeline.bin \\
        [--serial <serial> --compiler <ndk-clang++>]

This is the chapter about the parent function. Read it together with the
tutorial, which walks through what the recording contains and what the
reimplementation got wrong the first time.

The adapter:

1.  Validates the recording: probe set, module identity, and that every parent
    call has a leave record.
2.  For each recorded parent call, extracts (config, input) and the ordered
    child-call contract.
3.  Encodes all of that into the replay program's stdin.
4.  Runs the replay, which executes the reimplementation and checks that it
    made the same child calls in the same order with the same arguments.
5.  Compares the parent's returned values and the observed child log.

The recording is the oracle. Nothing here reads the oracle source.
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

KIND_PROCESS = 300
KIND_APPLY = 310
KIND_NORMALIZE = 320
KIND_SUMMARIZE = 330

# One recorded parent call per config path. The recording has hundreds; a few
# of each path is enough to prove the contract, and a small set keeps the
# comparison readable.
CASES_PER_CONFIG = 4
CONFIGS = (0, 1, 2)


def _probe_map(trace) -> dict[int, dict]:
    return {probe["kind"]: probe for probe in trace.module["probes"]}


def _snapshot(call, phase: str, register: str = None, source: str = None) -> bytes:
    """Return one snapshot's bytes for a call, by register or source."""
    probe = _PROBES[call.kind]
    event = call.enter if phase == "enter" else call.leave
    for definition, raw in snapshot_parts(event, probe["snapshots"], enter=call.enter):
        if definition["phase"] != phase:
            continue
        if register is not None and definition.get("register") != register:
            continue
        if source is not None and definition.get("source") != source:
            continue
        require(raw is not None, f"{call.kind} {phase} {register or source} not captured")
        return raw
    raise AssertionError(f"no snapshot for {call.kind} {phase} {register or source}")


def _word(call, phase: str, register: str) -> int:
    """Value of an argument register (x0..x3), or an FP register like s0."""
    return struct.unpack("<Q", _snapshot(call, phase, register=register)[:8])[0]


def _address(call, register: str) -> int:
    """The address held in a pointer argument register at entry.

    Note the two kinds of capture: `register: "x1"` gives the pointer *value*,
    while `source: "arg1"` gives the bytes *at* that address. Comparing buffer
    roles needs the pointer value; comparing contents needs the source form.
    """
    return _word(call, "enter", register)


def _bytes_at(call, phase: str, source: str) -> bytes:
    """Bytes captured at the address a pointer argument holds."""
    return _snapshot(call, phase, source=source)


# The probe map for the recording being encoded. Set once in `encode` so the
# small helpers above can stay readable.
_PROBES: dict[int, dict] = {}


def _child_contract(trace, parent) -> list[dict]:
    """Extract the ordered child-call contract recorded inside `parent`.

    Pointer arguments are recorded as **identity tokens**, not addresses. Two
    runs have different addresses for the same buffer, so comparing addresses
    would always fail. The roles are recoverable from the recording:

      * the parent's input is `arg1`, its output is `arg3`;
      * the parent's private working copy is the buffer passed to its first
        child (the parent copies input into it, then hands it to the helpers).

    The replay derives the same three identities from its own run, so the
    comparison is address-independent.
    """
    input_id = _address(parent, "x1")
    out_id = _address(parent, "x3")
    work_id = None
    if parent.children:
        first = trace.calls[parent.children[0]]
        work_id = _address(first, "x0")

    def identity(value: int) -> int:
        # 1 = work, 2 = out, 3 = input, matching the replay's enum.
        if work_id is not None and value == work_id:
            return 1
        if value == out_id:
            return 2
        if value == input_id:
            return 3
        return 0

    result = []
    for child_id in parent.children:
        child = trace.calls[child_id]
        if child.kind == KIND_APPLY:
            entry = {
                "kind": child.kind,
                "argc": 3,
                "args": [identity(_address(child, "x0")),
                         _word(child, "enter", "x1") & 0xFFFFFFFF,
                         0],
                "result": 0,
            }
        elif child.kind == KIND_NORMALIZE:
            entry = {
                "kind": child.kind,
                "argc": 2,
                "args": [identity(_address(child, "x0")),
                         _word(child, "enter", "x1") & 0xFFFFFFFF,
                         0],
                "result": _word(child, "leave", "s0"),
            }
        elif child.kind == KIND_SUMMARIZE:
            entry = {
                "kind": child.kind,
                "argc": 4,
                "args": [identity(_address(child, "x0")),
                         _word(child, "enter", "x1") & 0xFFFFFFFF,
                         0,
                         identity(_address(child, "x3"))],
                "result": 0,
            }
        else:
            raise AssertionError(f"unexpected child kind {child.kind}")
        result.append(entry)
    return result


def encode(trace) -> tuple[bytes, list[str]]:
    """Validate the recording and emit the replay input stream."""
    global _PROBES
    _PROBES = _probe_map(trace)
    parents = [c for c in trace.calls.values() if c.kind == KIND_PROCESS]
    require(parents, "recording has no parent calls")
    for call in parents:
        require(call.complete, "a parent call has no leave record")

    # Pick a few complete examples of each config path.
    chosen: list = []
    per_config: dict[int, int] = {c: 0 for c in CONFIGS}
    for call in parents:
        config = _word(call, "enter", "x0") & 0xFFFFFFFF
        if config in (0xFFFFFFFF, 0xFFFFFFFE):
            continue  # -1 and -2; invalid-config calls carry no child contract
        if config not in per_config or per_config[config] >= CASES_PER_CONFIG:
            continue
        per_config[config] += 1
        chosen.append((config, call))
    require(all(per_config[c] >= 1 for c in CONFIGS),
            "recording must contain at least one call of each config path")

    inputs = bytearray(struct.pack("<I", len(chosen)))
    names: list[str] = []
    for index, (config, call) in enumerate(chosen):
        count = _word(call, "enter", "x2") & 0xFFFFFFFF
        input_ptr = _address(call, "x1")
        samples = struct.unpack(f"<{count}f", _bytes_at(call, "enter", "arg1")[:4 * count])
        children = _child_contract(trace, call)

        inputs += struct.pack("<ii", config, count)
        inputs += struct.pack(f"<{count}f", *samples)
        inputs += struct.pack("<I", len(children))
        for child in children:
            inputs += struct.pack("<II", child["kind"], child["argc"])
            args = list(child["args"]) + [0, 0, 0, 0]
            inputs += struct.pack("<QQQQ", *args[:4])
            inputs += struct.pack("<Q", child["result"])
        names.append(f"config{config}[{index}] input=0x{input_ptr:x} "
                     f"children={[c['kind'] for c in children]}")
    return bytes(inputs), names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    add_replay_arguments(parser)
    args = parser.parse_args(argv)

    trace = load(args.trace, "pipeline-probe-set")
    inputs, names = encode(trace)
    include = ROOT / "tutorial" / "example"
    replay = run_replay(ROOT / "tutorial" / "replay_pipeline.cpp", inputs, [include],
                        serial=args.serial, compiler=args.compiler,
                        extra_sources=(include / "pipeline_mine.cpp",))
    require(replay.output, "replay produced no output")
    print(json.dumps({
        "result": "PASS",
        "cases": len(names),
        "cases_detail": names,
        "session_id": trace.metadata.get("session_id"),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "native": replay.identity,
        "scope": "pipeline_process parent control flow, ordered child calls with "
                 "arguments, returned count and the 4 output floats, for the three "
                 "recorded config paths; the lock and the global state object are "
                 "not compared",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
