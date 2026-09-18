# rr-frida

A record/replay oracle for native shared libraries.

You reimplemented a function from a stripped `.so`. It compiles and looks right.
`rr-frida` records what the original actually did, then replays that recording
against your code and compares the result **byte for byte** — so a one-ULP
rounding difference or a swapped field is caught instead of shipped.

```text
record  (Frida)   official library -> trace file (calls, args, returns, memory)
validate          independent checks against the official evidence
encode            trace -> typed input stream for your reimplementation
replay  (native)  run your C++ on host or device
compare           your output vs the recorded output, byte for byte
```

## Why it works without ABI compatibility

The recording is a **contract**, not an execution trace. It stores which
function ran, its arguments, its return value, selected memory before and after,
and its ordered child calls. Replay runs *your* implementation with the recorded
arguments and compares observable business state. Your code does not have to
share the original's layout, calling convention, or object model.

## What the tutorial covers

[`tutorial/README.md`](tutorial/README.md) is a four-case investigation, not a
feature tour. Each case builds a method that catches one class of failure, then
watches a harder class walk past it:

- **Case A** — a leaf function whose result differs in the last bit. Value
  comparison catches it, if the input can expose it.
- **Case B** — the input you did not think to try. A passive recording inherits
  the driver's blind spots, so a **fixture** takes control and calls the original
  directly with chosen inputs. Its answers are re-checked against the trace.
- **Case C** — a parent function whose code paths are distinguishable only by
  their **ordered child calls**. The method has to compare a call contract,
  normalize pointers to identities, and provide dependencies as checked shims.
- **Case D** — the lock and global state held but not compared, and why that
  judgement is the hardest part of the method.

Plus the part that is easy to skip: **a broken recording is worse than no
recording**, and the four integrity checks that stop one being written.

Every step runs on a real device. Deliberately injected bugs are caught, then
fixed.

## Install

Python 3.10+ and the `frida` Python package for recording. Replay needs a C++
compiler, plus an Android NDK toolchain to run on a device.

```bash
pip install frida==16.7.19
```

The client version must match the `frida-server` running on the device.

## Quick start

```bash
# record the tutorial example from a device
python3 -m rrfrida.record --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin

# inspect it
python3 -m rrfrida.inspect build/demo.bin --snapshots --limit 1

# check a reimplementation against it
python3 tutorial/compare_geom.py build/demo.bin \
    --serial <serial> --compiler "$ANDROID_NDK_HOME/.../clang++.exe"
```

Then follow [`tutorial/README.md`](tutorial/README.md), which explains each step
and the failure the pipeline catches.

## Tools

| Command | Purpose |
| --- | --- |
| `python3 -m rrfrida.record` | Attach Frida, record a call contract |
| `python3 -m rrfrida.inspect` | Print a trace as readable JSON |
| `python3 -m rrfrida.run` | Run a registered case from `cases.json` |
| `python3 tools/make_probes.py` | Generate probe declarations from a library |

## Probe and probe-set files

`probes/*.json` describe what to observe; `probe-sets/*.json` name the module
and pin its identity. See the module docstring in
[`rrfrida/probes.py`](rrfrida/probes.py) for the full field list, including the
float/integer register distinction that trips people up on AArch64.

## Scope

`rr-frida` compares the behaviour the recording covers. It does not prove
equivalence for inputs you never recorded, and it does not stand in for
integration testing, GPU output, or timing behaviour. Every result prints a
`scope` field saying what was actually checked.

## License

MIT. See [LICENSE](LICENSE).
