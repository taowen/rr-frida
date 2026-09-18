# Tutorial: prove a reimplementation matches the original

This tutorial builds a small native library, records its behaviour from a
running process on an Android device, and then checks a from-scratch
reimplementation against that recording **byte for byte** — catching a one-ULP
floating-point difference that a normal test would miss.

The example is deliberately tiny. The point is the pipeline, not the functions.

---

## The problem this solves

You rewrote a function from a stripped `.so`. It compiles. It produces results
that look right. How do you know it is right?

"Looks right" fails here, because the failure mode is a last-bit difference:

```
official: dir = 0.26726123690605164, 0.5345224738121033, 0.8017836809158325
mine:     dir = 0.26726123690605164, 0.5345224738121033, 0.8017837405204773
                                                                      ^ one ULP
```

The cause in this example is that the official code computes a length in
`float` while the reimplementation used `double`. Both are "correct" math; only
one matches the original. Tests that compare with a tolerance will never notice,
and the difference compounds downstream.

The fix is not a better assertion. It is a **recording of what the original
actually did**, replayed against your code.

---

## Why this works without ABI compatibility

The reimplementation is ordinary C++. It does not share the official memory
layout, calling convention, or object model. That is fine, because the recording
stores a **contract**, not an execution:

- which function was called, on which thread
- the argument values and the return value
- selected memory regions before and after
- the ordered child calls

Replay runs *your* function with the recorded arguments and compares the
observable result. Two implementations agree when their business-visible state
agrees — regardless of how they are built inside.

Comparison happens on **normalized** state: absolute addresses are mapped to
object identities, and allocator/runtime internals are excluded. For this
tutorial every value is a float, so no normalization is needed.

---

## Layout

```
tutorial/
  example/
    geom.h            the interface
    official.cpp      the original, built into libgeom.so
    mine.cpp          your reimplementation
    driver.cpp        an Android process that calls the library in a loop
    probe-set.json    which module to attach to, pinned by sha256
  replay_geom.cpp     reads the recording's inputs, calls mine.cpp
  compare_geom.py     validate -> encode -> replay -> compare
probes/
  geom.json           where the functions are and what to snapshot
```

---

## Step 1 — build the official library and a driver

The recording needs a live process that calls the library. Build the library as
a **shared object** (so Frida can find it by module name) and a small driver that
links it:

```bash
NDK=$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/windows-x86_64/bin
$NDK/clang++ --target=aarch64-linux-android29 -O2 -shared -fPIC \
    tutorial/example/official.cpp -o build/libgeom.so
$NDK/clang++ --target=aarch64-linux-android29 -O2 -static-libstdc++ \
    -Wl,-rpath,/data/local/tmp -Lbuild -lgeom \
    tutorial/example/driver.cpp -o build/geom-driver
```

`driver.cpp` loops forever calling both functions with fixed inputs. It prints a
line every 100000 iterations and otherwise sleeps, so the call rate stays low
enough for the recorder.

> The inputs are chosen so the bug is observable. `geom_direction(1,2,3)` is a
> case where `float` and `double` lengths round differently. If you changed it
> to `(1,2,2)` the two implementations would agree and the tutorial would not
> demonstrate anything. Choosing an input that exercises the difference is part
> of the job — the recording can only prove what it covers.

## Step 2 — generate the probe declarations

Hand-writing RVAs and instruction bytes is error prone, so derive them from the
library you just built:

```bash
python3 tools/make_probes.py build/libgeom.so \
    --nm "$NDK/llvm-nm.exe" \
    --function geom_scale:100 --function geom_direction:200 \
    --snapshot "geom_scale=enter:arg0:8" ... \
    --output probes/geom.json
```

The committed `probes/geom.json` is the result, with one correction a human had
to make. `make_probes.py` cannot know which arguments are pointers, so it
initially produced snapshots that dereferenced the *float* arguments — which
crashed the agent. The corrected file uses **register snapshots** for floats:

```json
"geom_scale": {
  "kind": 100, "rva": "0x45f8", "expected": "02102e1e0208011f",
  "snapshots": [
    { "phase": "enter", "register": "s0" },
    { "phase": "enter", "register": "s1" },
    { "phase": "enter", "source": "arg0", "size": 8 },
    { "phase": "leave", "register": "s0" },
    { "phase": "leave", "source": "arg0", "size": 8 }
  ]
}
```

Two rules this exposes, both learned the hard way:

- **`s0`..`s3` are not `arg0`..`arg3`.** AArch64 passes floats in the S registers
  and integers/pointers in the X registers. Frida's `args[]` array is the X
  bank. `geom_scale(float a, float b, float* out)` has `out` in `arg0`, not
  `arg2`. Reading `arg2` dereferences leftover integer garbage and crashes.
- **Frida reports an S register by value, not by bit pattern.** `s0` for `2.0f`
  comes back as the number `2`. The agent re-encodes FP registers as IEEE-754
  bits (`registerBytes` in `agent.js`) so the snapshot is comparable.

`expected` pins the first eight bytes at the entry. The agent refuses to attach
if they do not match, so a recording can never silently come from another build.

## Step 3 — pin the module identity

```bash
python3 -c "import hashlib,json,pathlib; \
  print(hashlib.sha256(pathlib.Path('build/libgeom.so').read_bytes()).hexdigest())"
```

Put the hash in `tutorial/example/probe-set.json`. Pin exactly one of `sha256`
or `build_id`. A library with no GNU Build ID note must use `sha256`.

## Step 4 — record on the device

```bash
adb push build/libgeom.so build/geom-driver /data/local/tmp/
adb shell chmod 755 /data/local/tmp/geom-driver
adb shell "nohup /data/local/tmp/geom-driver >/dev/null 2>&1 &"

python3 -m rrfrida.record \
    --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin
```

Requirements:

- A `frida-server` on the device whose version matches the local `frida`
  package. On Android it must run as root.
- The driver must still be alive when the recorder attaches.

The recorder verifies module identity and instruction bytes, attaches, observes
for the duration, flushes, and writes the trace. It fails if the agent dropped
any events.

## Step 5 — look at what was recorded

```bash
python3 -m rrfrida.inspect build/demo.bin --snapshots --limit 1
```

```json
{
  "kind": 100, "phase": "complete",
  "args": ["0x7fc1cd17b8", "0x7fc1cd1780", "0x37", "0xffffffffffffffff"],
  "retval": "0x7fc1cd17b8",
  "enter_snapshots": [
    { "source": "s0",   "hex": "0000004000000000" },
    { "source": "s1",   "hex": "0000404000000000" },
    { "source": "arg0", "hex": "0000e040000080bf" }
  ]
}
```

- `s0` = `0x40000000` = `2.0f`, `s1` = `0x40400000` = `3.0f` — the arguments.
- `arg0` = `7.0f, -1.0f` — `out[0] = 2*3+1`, `out[1] = 2-3`. The pointer is
  `arg0` because the floats took the S registers.

Note the `args` array itself holds stale integer registers. That is exactly the
trap: the useful data is in the named snapshots, not in `args`.

## Step 6 — compare your reimplementation

```bash
python3 tutorial/compare_geom.py build/demo.bin \
    --serial <serial> --compiler "$NDK/clang++.exe"
```

With the buggy `mine.cpp` (double-precision length) this fails with **the first
differing byte**:

```
rrtrace.format.TraceError: direction[0] byte 8: actual 0xb3 != official 0xb2
```

After fixing the length to `float`, the same recording passes:

```json
{ "result": "PASS", "cases": 64, "bytes": 768,
  "scope": "geom_scale and geom_direction with the recorded argument values; ..." }
```

`compare_geom.py` performs four steps in order, and the order matters:

1. **Validate** the recording against the official evidence — probe set, module
   identity, call shape, argument ABI, snapshot sizes. This layer is never
   relaxed to make an implementation pass.
2. **Encode** the validated inputs into the replay program's stdin.
3. **Replay** — compile `replay_geom.cpp` plus `mine.cpp`, run on the device.
4. **Compare** the reimplementation's stdout to the recorded result, bit by bit.

---

## What the recording does and does not prove

The recording covers **one fixed input** per function, repeated. It proves that
your reimplementation matches the official one *for those inputs*. It does not
prove equivalence for inputs the driver never used.

That is why the adapter prints a `scope` field, and why the input choice is
documented next to the driver. When you extend the driver to vary its inputs,
you get more coverage from the same pipeline. The recording is the asset: add
cases, re-record, and every future reimplementation is checked against them.

## Common failures

| Symptom | Cause |
| --- | --- |
| `unable to connect to remote frida-server` | No `frida-server` running, or a version mismatch with the local client |
| `installation mismatch at 0x...` | Probes point at a different build; regenerate with `make_probes.py` |
| Agent `access violation` on `captureSnapshots` | A snapshot dereferenced a non-pointer argument (see the float/register note) |
| `leave without matching enter` | The process exited mid-observation, or a probe hooked the wrong address |
| First diff in an output field | The reimplementation differs; that is the tool working |
