"""Generate probe declarations from a compiled library.

Hand-writing `rva` and `expected` bytes is error prone. This tool reads the
symbol table and the first instruction bytes so the declaration matches the
build you are actually going to record.

    python3 tools/make_probes.py build/libgeom.so \\
        --function geom_scale:100,geom_direction:200 \\
        --snapshot geom_scale=arg0:4 --snapshot geom_scale=arg1:4 \\
        --output probes/geom.json

Snapshots are written only for the functions you name, so the output stays
small and reviewable. The emitted file still has to be checked by a human: the
tool cannot know which arguments are pointers, so every `source` you pass is
taken at face value.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import subprocess
import sys


def read_symbols(library: Path, nm: str) -> dict[str, int]:
    """Return function name -> RVA for every defined function symbol.

    Use an `nm` from the same toolchain that linked the library. A host `nm`
    may not understand the target's ELF at all, or may report addresses in a
    different form.
    """
    output = subprocess.run([nm, "--defined-only", "--format=posix", str(library)],
                            check=True, capture_output=True, text=True).stdout
    symbols: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, kind, address = parts[0], parts[1], parts[2]
        if kind not in ("T", "t", "W"):
            continue
        symbols[name] = int(address, 16)
    return symbols


def load_segments(library: Path) -> list[tuple[int, int, int]]:
    """Return (vaddr, file_offset, file_size) for every PT_LOAD segment.

    A shared object commonly has several PT_LOAD segments at different
    virtual addresses. Mapping an RVA to a file offset must use the segment
    that actually contains the address, not just the lowest one.
    """
    data = library.read_bytes()
    header = data[:64]
    if header[:4] != b"\x7fELF":
        raise SystemExit(f"{library} is not an ELF image")
    if header[4] != 2:
        raise SystemExit(f"{library} is not a 64-bit ELF image")
    phoff = struct.unpack_from("<Q", header, 0x20)[0]
    phentsize = struct.unpack_from("<H", header, 0x36)[0]
    phnum = struct.unpack_from("<H", header, 0x38)[0]
    segments = []
    for index in range(phnum):
        phdr = phoff + index * phentsize
        if struct.unpack_from("<I", data, phdr)[0] != 1:   # PT_LOAD
            continue
        offset = struct.unpack_from("<Q", data, phdr + 0x08)[0]
        vaddr = struct.unpack_from("<Q", data, phdr + 0x10)[0]
        filesz = struct.unpack_from("<Q", data, phdr + 0x20)[0]
        segments.append((vaddr, offset, filesz))
    if not segments:
        raise SystemExit("no PT_LOAD segment")
    return segments


def read_bytes(library: Path, segments: list[tuple[int, int, int]], rva: int,
               count: int) -> str:
    """Read `count` bytes at a virtual address, using the containing segment."""
    data = library.read_bytes()
    for vaddr, offset, filesz in segments:
        if vaddr <= rva < vaddr + filesz:
            file_offset = offset + (rva - vaddr)
            chunk = data[file_offset:file_offset + count]
            if len(chunk) != count:
                raise SystemExit(
                    f"only {len(chunk)} of {count} bytes available at {rva:#x}")
            return chunk.hex()
    raise SystemExit(f"address {rva:#x} is not inside any PT_LOAD segment")


def parse_function_specs(specs: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for spec in specs:
        name, _, kind = spec.partition(":")
        if not kind or not kind.isdigit():
            raise SystemExit(f"--function expects name:kind, got {spec!r}")
        result[name] = int(kind)
    return result


def parse_snapshot_specs(specs: list[str]) -> dict[str, list[dict]]:
    """Parse --snapshot name=phase:source:size[:optional].

    A source of ``register:<reg>`` emits a register snapshot instead of a
    memory snapshot; register snapshots take their width from the register and
    must not carry a size.

        --snapshot "f=enter:arg0:8"
        --snapshot "f=leave:register:s0"
    """
    result: dict[str, list[dict]] = {}
    for spec in specs or []:
        function, _, rest = spec.partition("=")
        phase, _, remainder = rest.partition(":")
        if not (function and phase in ("enter", "leave", "instruction")):
            raise SystemExit(f"--snapshot expects name=phase:source:size, got {spec!r}")
        if remainder.startswith("register:"):
            # register:<name>; the width comes from the register itself.
            register = remainder[len("register:"):]
            if not register:
                raise SystemExit(f"register snapshot needs a register name: {spec!r}")
            result.setdefault(function, []).append({"phase": phase, "register": register})
            continue
        source, _, tail = remainder.partition(":")
        size, _, flags = tail.partition(":")
        if not (source and size.isdigit()):
            raise SystemExit(f"--snapshot expects name=phase:source:size, got {spec!r}")
        result.setdefault(function, []).append({
            "phase": phase,
            "source": source,
            "size": int(size),
            **({"optional": True} if flags == "optional" else {}),
        })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("library", type=Path)
    parser.add_argument("--nm", default="nm",
                        help="nm executable from the library's toolchain "
                             "(e.g. NDK llvm-nm)")
    parser.add_argument("--function", action="append", required=True,
                        help="name:kind, repeatable")
    parser.add_argument("--snapshot", action="append",
                        help="name=phase:source:size[:optional], repeatable")
    parser.add_argument("--instruction-bytes", type=int, default=8,
                        help="how many leading bytes to pin as 'expected'")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    symbols = read_symbols(args.library, args.nm)
    functions = parse_function_specs(args.function)
    snapshots = parse_snapshot_specs(args.snapshot)
    segments = load_segments(args.library)

    declarations = {}
    for name, kind in functions.items():
        if name not in symbols:
            raise SystemExit(f"symbol {name!r} not found; have: "
                             + ", ".join(sorted(symbols)[:20]))
        rva = symbols[name]
        declarations[name] = {
            "kind": kind,
            "type": "function",
            "rva": hex(rva),
            "expected": read_bytes(args.library, segments, rva, args.instruction_bytes),
        }
        if name in snapshots:
            declarations[name]["snapshots"] = snapshots[name]
    args.output.write_text(json.dumps(declarations, indent=2) + "\n")
    print(f"wrote {args.output} with {len(declarations)} probes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
