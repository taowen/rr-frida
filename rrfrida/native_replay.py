"""Compile and execute a native replay program.

A replay program reads a typed input stream on stdin, runs **your** C++
implementation, and writes a result stream on stdout. This module owns only
compilation, transport, and execution identity. It never loads the official
shared library and never supplies expected output to the program.

The caller (an adapter) owns the input encoding and the comparison.

Host execution:

    run_replay(source, inputs, includes=[...])

Android execution (the produced binary must be an AArch64 ELF):

    run_replay(source, inputs, includes=[...], serial="...",
               compiler="/path/to/ndk/aarch64-linux-android29-clang++")

Running on Android matters when the code under test uses NEON or depends on the
target libc; a host build can disagree with the device even at -O2.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import uuid


@dataclass(frozen=True)
class ReplayResult:
    output: bytes
    identity: dict


def add_replay_arguments(parser) -> None:
    parser.add_argument("--serial", help="run the translated program on this Android device")
    parser.add_argument("--compiler",
                        help="C++ compiler; Android requires an explicit NDK AArch64 compiler")


def run_replay(source: Path, inputs: bytes, includes: list[Path], *,
               serial: str | None = None, compiler: str | None = None,
               link_flags: tuple[str, ...] = (),
               extra_sources: tuple[Path, ...] = ()) -> ReplayResult:
    if serial and not compiler:
        raise ValueError("--serial requires --compiler pointing at the NDK AArch64 clang++")
    compiler = compiler or "c++"
    with tempfile.TemporaryDirectory(prefix="rrfrida-replay-") as directory:
        binary = Path(directory) / "replay"
        command = [compiler, "-std=c++20", "-O2", "-pthread"]
        if serial:
            # The NDK's *-clang++.cmd wrapper adds --target itself, but calling
            # clang++.exe directly (which is more reliable from Python) needs it
            # spelled out. Adding it twice is harmless.
            command += ["--target=aarch64-linux-android29"]
            # Standalone fixture uses the NDK runtime statically, so it has no
            # dependency on the app's libc++_shared.so. This is not APK proof.
            command.append("-static-libstdc++")
        command += ["-I" + str(path) for path in includes]
        command += list(link_flags)
        command += [str(source), *(str(path) for path in extra_sources), "-o", str(binary)]
        subprocess.run(command, check=True)
        raw = binary.read_bytes()
        identity = {
            "target": "android-aarch64" if serial else "host",
            "compiler": compiler,
            "binary_sha256": hashlib.sha256(raw).hexdigest(),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "input_sha256": hashlib.sha256(inputs).hexdigest(),
            "compile_flags": command[1:-3],
        }
        if serial:
            if (len(raw) < 20 or raw[:6] != b"\x7fELF\x02\x01"
                    or struct.unpack_from("<H", raw, 18)[0] != 183):
                raise ValueError("Android replay must be a little-endian ELF64 AArch64 executable")
            output = _run_on_android(binary, inputs, serial, identity, directory)
        else:
            output = subprocess.run([str(binary)], input=inputs, check=True,
                                    capture_output=True).stdout
        identity["output_sha256"] = hashlib.sha256(output).hexdigest()
        return ReplayResult(output, identity)


def _run_on_android(binary: Path, inputs: bytes, serial: str, identity: dict,
                    directory: str) -> bytes:
    import subprocess as sp

    adb = ["adb", "-s", serial]
    remote = "/data/local/tmp/rrfrida-replay-" + uuid.uuid4().hex
    remote_input, remote_output = remote + ".input", remote + ".output"
    input_file = Path(directory) / "input"
    input_file.write_bytes(inputs)
    identity["serial"] = serial
    identity["device_fingerprint"] = sp.run(
        [*adb, "shell", "getprop", "ro.build.fingerprint"],
        check=True, capture_output=True, text=True).stdout.strip()
    try:
        sp.run([*adb, "push", str(binary), remote], check=True, capture_output=True)
        sp.run([*adb, "shell", "chmod", "700", remote], check=True, capture_output=True)
        sp.run([*adb, "push", str(input_file), remote_input], check=True, capture_output=True)
        # Shell v2 keeps binary stdout and propagates the exit status; a
        # file-backed stdin gives the program a real EOF.
        result = sp.run([*adb, "shell", "-T", f"{remote} < {remote_input} > {remote_output}"],
                        stdin=sp.DEVNULL, capture_output=True)
        if result.returncode:
            # Surface the program's first mismatch before the temp files vanish.
            print(result.stderr.decode(errors="replace"), file=sys.stderr)
            result.check_returncode()
        # Read the file through exec-out: some hosts translate newline bytes in
        # streamed shell stdout, which would corrupt a binary protocol.
        return sp.run([*adb, "exec-out", "cat", remote_output],
                      check=True, capture_output=True).stdout
    finally:
        sp.run([*adb, "shell", "rm", "-f", remote, remote_input, remote_output],
               check=True, capture_output=True)


def android_elf_check(path: Path) -> None:
    """Reject a binary that is not a little-endian ELF64 AArch64 executable."""
    raw = Path(path).read_bytes()[:20]
    if (len(raw) < 20 or raw[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<H", raw, 18)[0] != 183):
        raise ValueError(f"{path} is not a little-endian ELF64 AArch64 executable")


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_runtime_library(path: Path) -> None:
    if not path.is_file() or not _SAFE_NAME.match(path.name):
        raise ValueError(f"invalid runtime library: {path}")
