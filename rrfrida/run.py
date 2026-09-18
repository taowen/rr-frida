"""Case registry and runner.

`cases.json` names each case and points at its adapter:

    {
      "add_ints": {
        "probe_set": "demo",
        "compare": "tutorial/compare_add.py"
      }
    }

`python3 -m rrfrida.run add_ints --trace build/demo.bin --serial … --compiler …`
executes the adapter's `main()` against the trace. New cases are registered
here rather than growing a bespoke runner per case.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def load_cases(path: Path) -> dict:
    cases = json.loads(Path(path).read_text())
    if not isinstance(cases, dict) or not cases:
        raise ValueError("cases.json must be a non-empty object")
    for name, case in cases.items():
        if not isinstance(case, dict) or "compare" not in case:
            raise ValueError(f"case {name!r} must be an object with a 'compare' path")
    return cases


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=root / "cases.json")
    parser.add_argument("case", help="case name from cases.json")
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--serial")
    parser.add_argument("--compiler")
    parser.add_argument("--target", choices=("android", "host"), default="android")
    args, passthrough = parser.parse_known_args(argv)

    cases = load_cases(args.cases)
    if args.case not in cases:
        parser.error(f"unknown case {args.case!r}; known: {', '.join(sorted(cases))}")
    case = cases[args.case]
    command = [sys.executable, str(root / case["compare"]), str(args.trace),
               *case.get("args", []), *passthrough]
    if args.target == "android":
        if args.serial:
            command += ["--serial", args.serial]
        if args.compiler:
            command += ["--compiler", args.compiler]
    return subprocess.run(command, cwd=root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
