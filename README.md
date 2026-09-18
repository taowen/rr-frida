# rr-frida

**Differential testing for native shared libraries.** Record what an official
binary does, then check that your reimplementation does the same — byte for
byte, on the device.

## What this is, precisely

This is not ordinary **snapshot testing**, though it shares its shape. In
snapshot testing your own code writes the golden file, and the test proves you
have not *changed*. Here the golden file comes from a **different
implementation** — the original binary — and the test proves you are *equal to
it*. That is **differential testing**, with a recording as the reference.

Three properties follow, and they drive everything else in this repository:

1. **The reference is an artifact, not a specification.** You record the
   original while it runs, so the evidence is what it *did*, not what its
   documentation or disassembly *claims*.
2. **Comparison is on normalized business state.** The reference and your code
   have different addresses, allocators, and thread internals. Addresses become
   identities; platform internals are excluded.
3. **The reference must be reproducible.** Anything ambient the original read —
   wall-clock time, process state — has to be captured and replayed, or the
   comparison is meaningless.

## How it works

```text
record   (Frida)    oracle binary -> recording (calls, args, returns, memory)
validate            check the recording against the oracle's evidence, and itself
encode              recording -> typed input stream for your reimplementation
replay   (native)   run your C++ on host or device
compare             your output vs the recorded output, byte for byte
```

The central object is a **call contract**: for each hooked function, the
arguments, the return value, selected memory before and after, and the ordered
child calls. A recording stores contracts, not execution. That is why your code
does not need to share the original's ABI, layout, or object model.

### Glossary

| Term | Meaning |
| --- | --- |
| **oracle** | The reference implementation under test, or its recording. The thing you must match. |
| **recording** | The file produced by the recorder. Its technical format is a **trace**; `trace` is used for the file format and its reader, `recording` for the evidence as a whole. |
| **call contract** | What a recording stores for one function: inputs, output, memory, ordered children. |
| **probe** | A declaration of what to observe: which function, which arguments, which memory. |
| **probe set** | A named group of probes plus the module identity they belong to. |
| **fixture** | A recording driven by a script that calls the oracle directly with chosen inputs, instead of waiting for a process to call it. |
| **passive recording** | A recording made by observing a running process. Inherits the process's blind spots. |
| **adapter** | Per-case code that validates a recording, encodes it into replay input, and compares the result. |
| **normalization** | Turning addresses into identities and excluding platform state so two runs are comparable. |
| **scope** | The declared coverage of a PASS. Every result prints one. |

## What the tutorial covers

[`tutorial/README.md`](tutorial/README.md) is a five-case investigation, not a
feature tour. Each case builds a method that catches one class of failure, then
watches a harder class walk past it:

- **Case A** — a leaf function whose result differs in the last bit. Value
  comparison catches it, if the input can expose it.
- **Case B** — the input you did not think to try. A passive recording inherits
  the driver's blind spots, so a **fixture** takes control and calls the oracle
  directly with chosen inputs. Its answers are re-checked against the recording.
- **Case C** — the input you cannot see. `pipeline_predict` reads
  `CLOCK_MONOTONIC`, so it disagrees with itself between runs. The clock readings
  are captured and fed back, and the adapter's elapsed arithmetic has to match
  the oracle's float boundary exactly.
- **Case D** — a parent function whose code paths are distinguishable only by
  their **ordered child calls**. The method compares a call contract, normalizes
  pointers to identities, and supplies dependencies as checked shims.
- **Case E** — the lock and global state held but not compared, and why that
  judgement is the hardest part of the method.

Plus the part that is easy to skip: **a broken recording is worse than none**,
and the four integrity checks that stop one being written.

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
| `python3 -m rrfrida.record` | Attach Frida, make a passive recording |
| `python3 -m rrfrida.fixture` | Run a fixture and record the oracle it calls |
| `python3 -m rrfrida.inspect` | Print a recording as readable JSON |
| `python3 -m rrfrida.run` | Run a registered case from `cases.json` |
| `python3 tools/make_probes.py` | Generate probe declarations from a library |

## Probes and probe sets

`probes/*.json` declare what to observe; `probe-sets/*.json` name the module and
pin its identity. See the module docstring in
[`rrfrida/probes.py`](rrfrida/probes.py) for the full field list, including the
float/integer register distinction that trips people up on AArch64.

## Scope

`rr-frida` compares the behaviour the recording covers. It does not prove
equivalence for inputs you never recorded, and it does not stand in for
integration testing, GPU output, or timing behaviour. Every result prints a
`scope` field saying what was actually checked.

## License

MIT. See [LICENSE](LICENSE).
