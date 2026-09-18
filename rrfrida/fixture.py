"""Run a fixture that calls the oracle directly.

Passive recording waits for a real pipeline to call the function, which means
you record whatever the process happens to do. A **fixture** takes control: it
allocates the objects it needs, calls the oracle's entry point through a
`NativeFunction` with inputs it chooses, and reads the results out. That turns
recording from "watch what happens" into "ask this specific question".

    python3 -m rrfrida.fixture --serial <serial> --process geom-driver \\
        --probe-set tutorial/example/probe-set.json --probes-dir probes \\
        --fixture fixtures-add.js --output build/add.bin

The fixture is a JavaScript module exporting::

    rpc.exports = {
      setup() { return {tid, regions: {name: {address, size}}}; },
      run()   { /* call the oracle entry point, return a result object */ },
    }

`setup` allocates memory and reports the regions it owns. The agent binds them
so recording is confined to the fixture's thread. `run` performs the calls; the
probes already installed on the oracle entry capture each one.

The fixture's returned object is a **claim**, not evidence. It is stored as
`fixture_result` and can be checked against the captured calls by a validator;
nothing downstream should treat it as observed truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from rrfrida.probes import load_catalog, load_probe_set
from rrfrida.record import (
    RecorderError,
    _find_process,
    _import_frida,
    _require_required_probes,
    build_agent_source,
    hash_agent_bundle,
)
from rrfrida.trace_writer import TraceWriter, WriteError


def run_fixture(args: argparse.Namespace) -> dict:
    frida = _import_frida()
    probe_set = load_probe_set(args.probe_set)
    names = list(probe_set["required"]) + list(probe_set.get("optional", []))
    probes = load_catalog(Path(args.probes_dir), names)
    fixture_source = Path(args.fixture).read_text(encoding="utf-8")

    device = (frida.get_device(args.serial, timeout=5) if args.serial
              else frida.get_usb_device(timeout=5))
    pid = args.pid if args.pid else _find_process(device, args.process)
    session = device.attach(pid)

    writer = TraceWriter()
    # The fixture's rpc.exports must win over the agent's, so the fixture source
    # is appended after the agent and redefines the exports it needs. The
    # fixture's setup/run are exposed alongside the agent's control RPCs.
    agent_source = build_agent_source(probes, probe_set, args.session_id)
    source = agent_source + "\n" + (
        "const __agentExports = rpc.exports;\n"
        "rpc.exports = Object.assign({}, __agentExports, (function(){\n"
        + fixture_source +
        "\nreturn rpc.exports; })());\n")
    script = session.create_script(source, name="rrfrida-fixture")
    script.on("message", lambda message, data: writer.on_message(message, data))
    script.load()

    deadline = time.monotonic() + args.install_timeout
    while writer.installed_status is None and writer.install_error is None:
        if time.monotonic() >= deadline:
            raise RecorderError("agent did not install in time")
        time.sleep(0.05)
    if writer.install_error:
        raise RecorderError(writer.install_error)

    setup = script.exports_sync.setup()
    regions = setup.get("regions") or {}
    bound = script.exports_sync.bindfixturememory({"tid": setup["tid"], "regions": regions})
    # Capture clock readings the target makes, so a time-dependent function can
    # be reproduced. This only observes CLOCK_MONOTONIC calls made from the
    # target module; it never changes a value.
    if args.capture_clock:
        script.exports_sync.enableclockcapture()
    script.exports_sync.beginobservation()
    result = script.exports_sync.run()
    clock_readings = (script.exports_sync.clockreadings()
                      if args.capture_clock else [])
    summary = script.exports_sync.finishobservation()
    if writer.error:
        raise RecorderError(f"agent error during fixture: {writer.error}")
    script.unload()

    module = writer.installed_status["module"]
    metadata = {
        "schema": 1,
        "session_id": args.session_id,
        "probe_set": probe_set,
        "agent_summary": {"module": module, "finish": summary},
        "agent_bundle_sha256": hash_agent_bundle(),
        "frida_version": summary.get("frida_version"),
        "process": {"name": args.process, "pid": pid},
        "fixture_memory": {"tid": setup["tid"], "regions": regions},
        "fixture_result": result,
        "clock_readings": clock_readings,
    }
    manifest = writer.finish(Path(args.output), metadata)
    if summary.get("drop_count"):
        raise RecorderError(f"agent dropped {summary['drop_count']} events")
    _require_required_probes(probe_set, manifest["probe_counts"], names, probes)
    return {
        "trace": str(args.output),
        "records": manifest["records"],
        "bytes": manifest["bytes"],
        "session_id": args.session_id,
        "module": module["name"],
        "module_sha256": module.get("sha256"),
        "fixture_keys": sorted(result.keys()) if isinstance(result, dict) else [],
        "clock_readings": len(clock_readings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe-set", required=True, type=Path)
    parser.add_argument("--probes-dir", default="probes", type=Path)
    parser.add_argument("--fixture", required=True, type=Path,
                        help="JavaScript fixture with rpc.exports setup/run")
    parser.add_argument("--serial")
    parser.add_argument("--process")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--session-id")
    parser.add_argument("--install-timeout", type=float, default=10.0)
    parser.add_argument("--capture-clock", action="store_true",
                        help="record CLOCK_MONOTONIC readings made by the target module")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.process and not args.pid:
        parser.error("one of --process or --pid is required")
    if not args.session_id:
        args.session_id = time.strftime("rrf-%Y%m%d-%H%M%S")
    try:
        result = run_fixture(args)
    except (RecorderError, WriteError) as error:
        print(f"fixture failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
