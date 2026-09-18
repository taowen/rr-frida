"""Record a call contract from a running process with Frida.

    python3 -m rrfrida.record --serial <serial> --process <name> \\
        --probe-set demo --duration 3 --output build/demo.bin

The recorder attaches to an already-running process, injects the agent, waits
for it to install, lets it observe for ``--duration`` seconds, then flushes and
writes a trace file. It never launches or modifies the target beyond attaching.

Attaching requires a Frida server on the device that matches the version of the
local ``frida`` package. On Android that usually means pushing a matching
``frida-server`` binary and running it as root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from rrfrida.probes import load_catalog, load_probe_set
from rrfrida.trace_writer import TraceWriter, WriteError


class RecorderError(RuntimeError):
    """The environment or the target prevented a valid recording."""


PACKAGE_ROOT = Path(__file__).resolve().parent


def build_agent_source(probes: dict, probe_set: dict, session_id: str) -> str:
    """Concatenate the agent pieces with their configuration.

    Order matters: layouts and probe catalog are globals the agent reads when
    it loads.
    """
    config = {"probeSet": probe_set, "probeCatalog": probes, "sessionId": session_id}
    parts = ["globalThis.RRFridaConfig = "
             + json.dumps({"probeSet": probe_set, "sessionId": session_id}) + ";\n",
             "globalThis.RRFridaProbeCatalog = " + json.dumps(probes) + ";\n"]
    for name in ("module-info.js", "layouts.js"):
        parts.append((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
    parts.append((PACKAGE_ROOT / "agent.js").read_text(encoding="utf-8"))
    return "\n".join(parts)


def hash_agent_bundle() -> str:
    """Hash the recorder sources so a trace records what recorded it."""
    digest = hashlib.sha256()
    for name in ("module-info.js", "layouts.js", "agent.js"):
        path = PACKAGE_ROOT / name
        relative = name.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "little"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "little"))
        digest.update(content)
    return digest.hexdigest()


def _import_frida():
    try:
        import frida  # type: ignore
    except ImportError as error:  # pragma: no cover - environment dependent
        raise RecorderError("the 'frida' Python package is required to record") from error
    return frida


def _find_process(device, name: str) -> int:
    matches = [p for p in device.enumerate_processes() if p.name == name]
    if not matches:
        names = sorted({p.name for p in device.enumerate_processes()})
        raise RecorderError(f"no process named {name!r}; running: {', '.join(names[:40])}")
    if len(matches) > 1:
        raise RecorderError(f"{len(matches)} processes named {name!r}; use --pid")
    return matches[0].pid


def _require_required_probes(probe_set: dict, counts: dict, names: list[str],
                             probes: dict) -> None:
    """Fail a recording in which a required probe never fired.

    A probe set marks probes required. If one never fired, the recording says
    nothing about it, and accepting the file would let a later comparison claim
    coverage it does not have.
    """
    required_kinds = {probes[name]["kind"] for name in probe_set["required"]}
    for kind in sorted(required_kinds):
        entry = counts.get(str(kind))
        if not entry or (entry["enter"] == 0 and entry["hit"] == 0):
            raise RecorderError(
                f"required probe kind {kind} never fired; recording proves nothing about it")
        if entry["enter"] and entry["enter"] != entry["leave"]:
            raise RecorderError(
                f"required probe kind {kind} has {entry['enter']} enters but "
                f"{entry['leave']} leaves")


def record(args: argparse.Namespace) -> dict:
    frida = _import_frida()
    probe_set = load_probe_set(args.probe_set)
    names = list(probe_set["required"]) + list(probe_set.get("optional", []))
    probes = load_catalog(Path(args.probes_dir), names)

    if args.serial:
        device = frida.get_device(args.serial, timeout=5)
    else:
        device = frida.get_usb_device(timeout=5)
    pid = args.pid if args.pid else _find_process(device, args.process)
    session = device.attach(pid)

    writer = TraceWriter()
    script = session.create_script(
        build_agent_source(probes, probe_set, args.session_id),
        name="rrfrida-agent")
    script.on("message", lambda message, data: writer.on_message(message, data))

    script.load()
    deadline = time.monotonic() + args.install_timeout
    while writer.installed_status is None and writer.install_error is None:
        if time.monotonic() >= deadline:
            raise RecorderError("agent did not install in time")
        time.sleep(0.05)
    if writer.install_error:
        raise RecorderError(writer.install_error)

    # Enable recording only once the host is receiving messages. Attaching and
    # installing can happen while the module is already executing calls.
    script.exports_sync.beginobservation()
    time.sleep(args.duration)
    summary = script.exports_sync.finishobservation()
    if writer.error:
        raise RecorderError(f"agent error during recording: {writer.error}")
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
    }
    manifest = writer.finish(Path(args.output), metadata)
    if summary.get("drop_count"):
        raise RecorderError(f"agent dropped {summary['drop_count']} events; recording invalid")
    _require_required_probes(probe_set, manifest["probe_counts"], names, probes)
    return {
        "trace": str(args.output),
        "records": manifest["records"],
        "bytes": manifest["bytes"],
        "session_id": args.session_id,
        "module": module["name"],
        "build_id": module.get("build_id"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe-set", required=True, type=Path,
                        help="probe-sets/<name>.json")
    parser.add_argument("--probes-dir", default="probes", type=Path,
                        help="directory of probes/*.json (default: probes)")
    parser.add_argument("--serial", help="Android device serial; omit for USB default")
    parser.add_argument("--process", help="process name to attach to")
    parser.add_argument("--pid", type=int, help="pid to attach to (overrides --process)")
    parser.add_argument("--session-id", help="label for this recording")
    parser.add_argument("--duration", type=float, default=3.0,
                        help="seconds to observe after the agent installs")
    parser.add_argument("--install-timeout", type=float, default=10.0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.process and not args.pid:
        parser.error("one of --process or --pid is required")
    if not args.session_id:
        args.session_id = time.strftime("rr-%Y%m%d-%H%M%S")
    try:
        result = record(args)
    except (RecorderError, WriteError) as error:
        print(f"recording failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
