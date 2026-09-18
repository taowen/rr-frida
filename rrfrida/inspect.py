"""Inspect a trace file.

    python3 -m rrfrida.inspect build/demo.bin
    python3 -m rrfrida.inspect build/demo.bin --snapshots

Prints the metadata, the call forest per thread, and optionally a hex dump of
each snapshot. This is the read side of the format: it never modifies the trace.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rrtrace.format import read_trace, snapshot_parts


def _format_call(trace, call, index: int, snapshots: bool) -> dict:
    view = {
        "index": index,
        "kind": call.enter.kind,
        "phase": "complete" if call.complete else "UNTERMINATED",
        "tid": hex(call.enter.tid),
        "seq": call.enter.sequence,
        "args": [hex(v) for v in call.enter.registers[:4]],
        "caller_lr": hex(call.enter.registers[4]),
    }
    if call.complete:
        view["retval"] = hex(call.leave.registers[0])
    if call.instructions:
        view["instructions"] = len(call.instructions)
    if call.children:
        view["children"] = [trace.calls[c].kind for c in call.children]
    if snapshots:
        definitions = _definitions(trace, call.enter.kind)
        view["enter_snapshots"] = _dump(call.enter, definitions)
        if call.complete:
            view["leave_snapshots"] = _dump(call.leave, definitions)
    return view


def _definitions(trace, kind: int) -> list[dict]:
    for probe in trace.module.get("probes", []):
        if probe["kind"] == kind:
            return probe.get("snapshots", [])
    return []


def _dump(event, definitions: list[dict]) -> list[dict]:
    if not definitions:
        return []
    try:
        parts = snapshot_parts(event, definitions)
    except ValueError as error:
        return [{"error": str(error)}]
    return [
        {
            "phase": definition["phase"],
            "source": definition.get("source") or definition.get("register"),
            "size": len(raw) if raw is not None else
                    (8 if "register" in definition else definition.get("size", 0)),
            "captured": raw is not None,
            "hex": raw.hex() if raw is not None else None,
        }
        for definition, raw in parts
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--snapshots", action="store_true", help="include snapshot hex")
    parser.add_argument("--limit", type=int, default=0, help="max calls per thread (0 = all)")
    args = parser.parse_args(argv)

    trace = read_trace(args.trace)
    module = trace.module
    report = {
        "session_id": trace.metadata.get("session_id"),
        "probe_set": trace.probe_set.get("name"),
        "module": {
            "name": module.get("name"),
            "build_id": module.get("build_id"),
            "sha256": module.get("sha256"),
        },
        "events": len(trace.events),
        "calls": len(trace.calls),
        "unscoped_instructions": len(trace.unscoped_instructions),
        "threads": {},
    }
    for tid, roots in sorted(trace.roots.items()):
        calls = [trace.calls[i] for i in roots]
        if args.limit:
            calls = calls[:args.limit]
        report["threads"][hex(tid)] = [
            _format_call(trace, call, i, args.snapshots) for i, call in enumerate(calls)
        ]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
