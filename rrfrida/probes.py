"""Probe and probe-set declarations.

A **probe** describes one function (or one instruction) to observe. A
**probe set** names the module to attach to, pins its identity, and lists which
probes must be present.

Files
-----

    probes/<name>.json        one or more probe definitions per file
    probe-sets/<name>.json    module identity plus required/optional probe names

Probe file shape::

    {
      "add_ints": {
        "kind": 100,
        "type": "function",
        "rva": "0x1120",
        "expected": "000800b9",
        "snapshots": [
          {"phase": "enter", "source": "arg0", "size": 8},
          {"phase": "leave", "source": "arg0", "size": 8}
        ]
      }
    }

Fields:

``kind``
    Integer id the recorder writes into each event. Adapters match on it.
``type``
    ``function`` (hook entry/leave), ``instruction`` (single hit), or ``got``
    (hook the target of a GOT slot, resolved at attach time).
``rva``
    Offset from the module base. Instruction offsets, not file offsets.
``expected``
    Hex of the first bytes at ``rva``. The recorder refuses to attach if they
    do not match, so a recording cannot silently come from a different build.
``snapshots``
    Ordered memory captures. ``phase`` is ``enter``/``leave``/``instruction``;
    ``source`` is ``argN``, ``entry_<reg>``, ``sp``, ``fp``, or a register name.
    ``size`` is the byte count, ``optional`` marks a capture allowed to fail.
``callerReturnRvas``
    Optional allow-list of return addresses. A call is only recorded when its
    caller returns to one of these RVAs, which filters unrelated callers.
``filter``
    Optional named predicate. See the agent for the supported values.

Probe set shape::

    {
      "module": "libdemo.so",
      "build_id": "…",
      "required": ["add_ints"],
      "optional": []
    }

Pin exactly one of ``build_id`` or ``sha256``. A module with no GNU Build ID
note must use ``sha256``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

PROBE_TYPE_FUNCTION = "function"
PROBE_TYPE_INSTRUCTION = "instruction"
PROBE_TYPE_GOT = "got"
PROBE_TYPES = {PROBE_TYPE_FUNCTION, PROBE_TYPE_INSTRUCTION, PROBE_TYPE_GOT}

SNAPSHOT_PHASES = {"enter", "leave", "instruction"}
_HEX = re.compile(r"^[0-9a-f]+$")
_BUILD_ID = re.compile(r"^[0-9a-f]{2,}$")


class DeclarationError(ValueError):
    """A probe or probe-set file is malformed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DeclarationError(message)


def load_probes(path: Path) -> dict[str, dict]:
    """Load one probes/*.json file into a name -> definition map."""
    declarations = json.loads(Path(path).read_text())
    _require(isinstance(declarations, dict) and declarations, "probe file must be a non-empty object")
    for name, definition in declarations.items():
        _validate_probe(name, definition)
    return declarations


def load_probe_set(path: Path) -> dict:
    """Load one probe-sets/*.json file and return its normalized form."""
    probe_set = json.loads(Path(path).read_text())
    for key in ("module", "required"):
        _require(key in probe_set, f"probe set missing {key!r}")
    _require(isinstance(probe_set["required"], list), "required must be a list")
    _require(isinstance(probe_set.get("optional", []), list), "optional must be a list")
    _require(bool(probe_set.get("build_id") or probe_set.get("sha256")),
             "probe set must pin build_id or sha256")
    if "build_id" in probe_set:
        _require(_BUILD_ID.match(probe_set["build_id"]) is not None, "invalid build_id")
    if "sha256" in probe_set:
        _require(re.fullmatch(r"[0-9a-f]{64}", probe_set["sha256"]) is not None, "invalid sha256")
    # The file stem is the probe-set name. Record it so a trace names the
    # probe set even after the file is renamed or moved.
    probe_set.setdefault("name", Path(path).stem)
    return probe_set


def load_catalog(probes_dir: Path, names: list[str]) -> dict[str, dict]:
    """Collect the named probes from every file under ``probes_dir``."""
    catalog: dict[str, dict] = {}
    for path in sorted(Path(probes_dir).glob("*.json")):
        for name, definition in load_probes(path).items():
            catalog[name] = definition
    missing = [name for name in names if name not in catalog]
    _require(not missing, f"probes not declared: {', '.join(missing)}")
    return {name: catalog[name] for name in names}


def _validate_probe(name: str, definition: dict) -> None:
    _require(isinstance(definition, dict), f"probe {name} must be an object")
    kind = definition.get("kind")
    _require(isinstance(kind, int) and not isinstance(kind, bool), f"probe {name} kind must be int")
    probe_type = definition.get("type")
    _require(probe_type in PROBE_TYPES,
             f"probe {name} type must be one of {sorted(PROBE_TYPES)}")
    rva = definition.get("rva")
    _require(isinstance(rva, str) and rva.startswith("0x"), f"probe {name} rva must be 0x-hex")
    _require(_HEX.match(rva[2:]) is not None, f"probe {name} rva must be hex digits")
    expected = definition.get("expected")
    if probe_type in (PROBE_TYPE_FUNCTION, PROBE_TYPE_INSTRUCTION):
        _require(isinstance(expected, str) and _HEX.match(expected) is not None
                 and len(expected) % 2 == 0 and len(expected) >= 8,
                 f"probe {name} needs at least four bytes of expected instruction")
    for snapshot in definition.get("snapshots", []):
        _validate_snapshot(name, snapshot)
    for rva_field in ("callerReturnRvas",):
        for value in definition.get(rva_field, []):
            _require(isinstance(value, str) and value.startswith("0x"),
                     f"probe {name} {rva_field} entries must be 0x-hex")


def _validate_snapshot(probe: str, snapshot: dict) -> None:
    _require(isinstance(snapshot, dict), f"probe {probe} snapshot must be an object")
    _require(snapshot.get("phase") in SNAPSHOT_PHASES,
             f"probe {probe} snapshot phase invalid")
    # A register snapshot names the register and takes its width from it. Any
    # other snapshot names a memory source and needs an explicit byte size, or
    # a dynamic size derived from an argument.
    if "register" in snapshot:
        _require(isinstance(snapshot["register"], str) and snapshot["register"],
                 f"probe {probe} snapshot register must be a non-empty string")
        _require("source" not in snapshot and "size" not in snapshot,
                 f"probe {probe} register snapshot must not also set source/size")
        return
    _require(isinstance(snapshot.get("source"), str),
             f"probe {probe} snapshot source must be a string")
    if "size_arg" in snapshot:
        _require(isinstance(snapshot["size_arg"], int) and snapshot["size_arg"] >= 0,
                 f"probe {probe} size_arg must be a non-negative int")
        _require(isinstance(snapshot.get("max"), int) and snapshot["max"] > 0,
                 f"probe {probe} dynamic snapshot needs a positive max")
        _require("size" not in snapshot,
                 f"probe {probe} dynamic snapshot must not also set size")
        _require(snapshot["phase"] == "enter",
                 f"probe {probe} dynamic snapshot must be captured on enter")
        return
    _require(isinstance(snapshot.get("size"), int) and snapshot["size"] >= 0,
             f"probe {probe} snapshot size must be a non-negative int")
