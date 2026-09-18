"""Shared validation helpers for adapters.

An *adapter* is the code that validates a recording, encodes it into a native
replay input, and compares the result. Before it does either, it must confirm
the recording is the one it claims to be. Those checks live here so every
adapter uses the same primitives and no adapter can quietly relax them.

The rule that makes this layer meaningful:

    Validate the recording against the oracle's evidence, never against your
    own implementation's output.

An adapter that changes a check so an existing recording passes is no longer
testing anything.
"""

from __future__ import annotations

from pathlib import Path

from rrtrace.format import Trace, expect_children, read_trace, require


def load(path: Path, probe_set: str, *, build_id: str | None = None,
         sha256: str | None = None) -> Trace:
    """Read a trace and assert its probe set and module identity."""
    trace = read_trace(path)
    require(trace.probe_set.get("name") == probe_set,
            f"expected probe set {probe_set!r}, got {trace.probe_set.get('name')!r}")
    module = trace.module
    if build_id is not None:
        require(module.get("build_id") == build_id,
                f"expected build {build_id}, got {module.get('build_id')}")
    if sha256 is not None:
        require(module.get("sha256") == sha256,
                f"expected sha256 {sha256}, got {module.get('sha256')}")
    return trace


def single_probe(trace: Trace, kind: int, count: int) -> list:
    """Assert a synchronous fixture: one thread, `count` complete calls of `kind`.

    Returns the calls in recording order. Use this when the probe is meant to be
    driven by a fixture that calls the function exactly `count` times on one
    thread; a stray call or a second thread means the recording is unusable.
    """
    probes = [p for p in trace.module.get("probes", []) if p.get("required")]
    require(len(probes) == 1 and probes[0]["kind"] == kind,
            "recording must contain exactly one required probe of the expected kind")
    require(len(trace.roots) == 1, "fixture must run on a single thread")
    tid = next(iter(trace.roots))
    calls = [trace.calls[i] for i in trace.roots[tid]]
    require(len(calls) == count and len(trace.calls) == count,
            f"expected {count} calls, found {len(calls)}")
    require(not trace.unscoped_instructions, "unexpected instruction records")
    for call in calls:
        require(call.complete, "every recorded call must have a leave record")
        expect_children(trace, call, [])
    return calls


def require_snapshot(call, phase: str, size: int) -> bytes:
    """Assert an enter/leave snapshot exists at the declared size and return it."""
    event = call.enter if phase == "enter" else call.leave
    require(event is not None, f"no {phase} record")
    require(len(event.snapshot) == size,
            f"{phase} snapshot is {len(event.snapshot)} bytes, expected {size}")
    require(event.flags & 1, f"{phase} snapshot was not captured")
    return event.snapshot
