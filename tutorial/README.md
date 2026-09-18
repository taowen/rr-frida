# The reimplementation that passed every test and was still wrong

You are given a stripped Android `.so`. One function in it, `pipeline_process`,
is the entry point your product calls. You have to rewrite it in your own C++.

You cannot call the library. You cannot link it. You have the decompiled body and
three helper functions it calls, all private. Your job is a new implementation
that behaves identically.

You write it. It compiles. It returns the right numbers on every input you try.

And it is wrong.

This tutorial is the hunt for that bug, and for the four more waiting behind it.
Each time you build a method that catches one class of failure, a harder class
walks past it. By the end you have a recorder, a replayer, and — the part that
actually matters — a way of knowing what your evidence *covers*.

Everything runs on a real device. Nothing is a thought experiment.

---

## What kind of testing this is

This is **differential testing**: the reference is another implementation, not a
specification. It looks like snapshot testing, and it is worth being precise
about the difference, because the difference is where all the difficulty lives.

| | snapshot testing | differential testing (here) |
| --- | --- | --- |
| Who writes the golden file | your own code | the **oracle** — the original binary |
| What it proves | you have not *changed* | you are *equal to* the reference |
| What is compared | one output value | a **call contract**: inputs, output, memory, ordered child calls |
| Can the golden file be compared directly | yes | no — addresses and platform state must be **normalized** |
| How the reference is obtained | incidental | deliberately, and it must be **reproducible** |

Two consequences run through every case:

- **The oracle is an artifact, not a specification.** You record what the
  original *did*. Documentation and disassembly are hints; the recording is
  evidence.
- **You own the inputs.** A recording only proves what it covers. If you did not
  choose what it covers, you do not know what it proves.

### Glossary

| Term | Meaning |
| --- | --- |
| **oracle** | The reference implementation, or its recording. The thing you must match. |
| **recording** | The evidence produced by the recorder. Its file format is a **trace**. |
| **call contract** | What a recording stores for one function: inputs, output, memory, ordered children. |
| **probe** | A declaration of what to observe. |
| **probe set** | A named group of probes plus the module identity they belong to. |
| **fixture** | A recording driven by a script that calls the oracle directly with chosen inputs. |
| **passive recording** | A recording made by observing a running process. Inherits its blind spots. |
| **adapter** | Per-case code that validates a recording, encodes it, and compares the result. |
| **normalization** | Turning addresses into identities, and excluding platform state, so two runs are comparable. |
| **scope** | The declared coverage of a PASS. |

---

## Cast of characters

| Piece | What it is |
| --- | --- |
| `libgeom.so` | The oracle for the warm-up cases. Two leaf functions. |
| `libpipeline.so` | The oracle with the parent function, its three private helpers, and a time-dependent function. |
| `driver.cpp` | A process that calls the oracle in a loop, so a **passive recording** has something to watch. |
| `fixtures/fixture_geom.js` | A **fixture** that calls the oracle directly, with inputs it chooses. |
| `mine.cpp`, `pipeline_mine.cpp` | Your reimplementations — the code under test. |
| `probes/*.json` | **Probe** declarations: which functions, which arguments, which memory. |
| `rr-frida` | The recorder, the replayer, and the **adapters**. |

The rule under everything: **a recording stores a call contract, not an
execution.** It records what was called and what the world looked like, not the
instructions. Neither side needs to resemble the other.

---

# Case A: the last bit that nobody sees

## The setup

```bash
pwsh -File tools/build-example.ps1
python3 tools/make_probes.py build/libgeom.so --nm "$NDK/llvm-nm.exe" ... \
    --output probes/geom.json
```

`geom_direction(float x, float y, float z, float* out)` normalizes a vector. Your
version computes the length in `double` and narrows at the end. The oracle
computes it in `float`.

You try `(1, 2, 2)`. It matches. A few more. They match. You ship.

## The evidence

Record the oracle while a driver calls it:

```bash
python3 -m rrfrida.record --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin
python3 tutorial/compare_geom.py build/demo.bin --serial <serial> --compiler "$NDK/clang++.exe"
```

```
rrtrace.format.TraceError: direction[0] byte 8: actual 0xb3 != official 0xb2
```

**One byte. The last byte of the third component.** One unit in the last place,
from a `float`/`double` rounding difference your inputs never exercised.

This is the argument for the method in one byte: **a recording is evidence of
what the oracle actually did, including the cases your intuition skipped.**

---

# Case B: the input you did not think to try

## The uncomfortable question

Case A caught the bug because the driver happened to use `(1, 2, 3)`. If it had
used only `(1, 2, 2)`, the recording would have blessed your broken code.

You did not choose those inputs. The driver did. That is a passive recording: it
records whatever the process happens to do, and you inherit both its coverage and
its blind spots.

So the real question is not "did the recording catch it". It is:

> What if the case I care about never happens on its own?

## Take control

A **fixture** calls the oracle directly. It allocates its own objects, calls
the entry through a `NativeFunction` with inputs it writes, and reads the results
out. Instead of hoping the process exercises a case, the fixture *asks*.

`tutorial/example/fixture_geom.js`:

```js
const direction = new NativeFunction(module.base.add(0x4610), 'void',
                                     ['float', 'float', 'float', 'pointer']);
const directionCases = [
  [1.0, 2.0, 3.0],        // float and double lengths differ here
  [1.0, 2.0, 2.0],        // ...and here they agree
  [-1.0, -2.0, -3.0],
  [1.0e-4, 2.0e-4, 3.0e-4],
];
for (const [x, y, z] of directionCases) {
  direction(x, y, z, output);
  results.push({kind: 'direction', input: [x, y, z], output: [...]});
}
```

Run it:

```bash
python3 -m rrfrida.fixture --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --fixture tutorial/example/fixture_geom.js --output build/fixture.bin
```

**14 records.** Seven cases, two events each (enter and leave). No driver, no
waiting, no luck.

## The mechanism

Two pieces make this safe:

**The agent binds the fixture's memory and confines recording to its thread.**

```js
bindfixturememory(binding) {
  if (fixtureMemory !== null) throw new Error('fixture memory already bound');
  for (const [name, region] of Object.entries(binding.regions)) {
    ptr(region.address).readByteArray(region.size);   // prove readable
  }
  fixtureMemory = binding;
}
```

The fixture reports the regions it allocated; the agent validates them as
readable and records only calls made from the fixture's thread. Without that
confinement a running process would mix its own activity into your recording.

**The fixture's answer is a claim, not evidence.** `run()` returns an object
with the results it read. That object goes into the trace as `fixture_result` —
and then the adapter checks it against the captured calls:

```python
def validate_fixture_claims(trace) -> int:
    for index, call in enumerate([*directions, *scales]):
        claim = results[index]
        out = _floats_at(call, "leave", "arg0", 3)
        require(list(claim["output"]) == list(out),
                f"fixture claim {index} disagrees with the captured output")
```

**Seven claims verified.** If the fixture had misread memory, or a probe had
captured something else, this check fails and the recording is rejected. A
fixture is trustworthy only because its claims are re-derived from the trace.

## The payoff

With the correct implementation, `compare_fixture.py` passes:

```json
{ "result": "PASS", "fixture_claims_checked": 7, "bytes": 84 }
```

Now break it. Put the `double` length back:

```
rrtrace.format.TraceError: byte 44: actual 0xb3 != official 0xb2
```

Caught — because the fixture *chose* `(1, 2, 3)`. The passive recording in Case A
only caught it by luck. The fixture catches it by design.

> The lesson is not "fixtures are better". It is that **you must own the input
> selection**. A recording proves what it covers; if you did not choose what it
> covers, you do not know.

---

# Case C: the input you cannot see

## A function that disagrees with itself

`pipeline_predict` reads `CLOCK_MONOTONIC` and returns the elapsed milliseconds
since its previous call, scaled by the input magnitude. Call it twice with the
same arguments and you get two different answers.

So record it the usual way and replay it:

```
record:  delta = 0        (first call, establishes the origin)
         delta = 130.94   (30 ms later)
         delta = 5.55     (1.3 ms later)
```

Your reimplementation does the same arithmetic. But it reads the clock too — and
by the time it runs, the machine is in a different state. The numbers are
different every time. The comparison can never pass, or worse, passes once by
coincidence and fails in production.

**A function that depends on ambient state cannot be replayed until that state
becomes part of the recording.**

## What the recorder captures

The agent hooks `clock_gettime` while the fixture runs and records every reading
the target module made:

```js
Interceptor.attach(clockGettime, {
  onLeave(retval) {
    if (this.clockId !== 1) return;                 // CLOCK_MONOTONIC only
    if (retval.toInt32() !== 0) return;
    const module = Process.findModuleByAddress(this.caller);
    if (module === null || module.name !== config.probeSet.module) return;
    readings.push({seconds: ..., nanos: ...});
  },
});
```

Three filters, each load-bearing:

- **Only `CLOCK_MONOTONIC`.** A recording has no business capturing the
  process's whole relationship with time.
- **Only successes.** A failed call leaves the destination untouched; recording
  it would invent a reading that never happened.
- **Only calls from the target module.** This is the one that matters most.
  Virtualizing the clock for the *whole process* hangs the host: the harness and
  the Android UI share the address space and rely on real timeouts. The real
  project documents exactly this — resolve the caller's module and leave
  everything else on real time.

The readings land in the trace as `clock_readings`, alongside `fixture_result`.

## What the adapter does with them

The replay must feed the function the time it originally saw. But "the elapsed
value" is not simply the difference between two readings — **the arithmetic is
part of the contract**:

```c
// official
float elapsed = (float)(now_ns - previous_ns) / 1000000.0f;
float delta   = elapsed * magnitude;
```

The nanosecond difference is narrowed to `float` **before** the division. Do the
same math in double and narrow at the end, and you are off by one ULP:

```
C path:      43 02 ef 86
double path: 43 02 ef 85     <- one byte
```

The adapter computes elapsed the way C does:

```python
values.append(_f32(_f32(float(difference_ns)) / _f32(1000000.0)))
```

> This is Case A's lesson returning in a new place. The float/double boundary is
> everywhere, including in your own adapter. The recording is the authority; the
> adapter's job is to interpret it exactly, not approximately.

## The replay

The reimplementation never reads a clock. It asks for the elapsed value:

```cpp
const float elapsed_ms = tutorial_helpers::predict_elapsed_ms();
```

The replay supplies it from the recording, one value per call. Run it:

```json
{ "result": "PASS", "cases": 3, "elapsed_ms": [0.0, 31.29, 1.35], "bytes": 36 }
```

The function is now reproducible. Its output depends only on recorded inputs:
the samples, and the time.

> The origin rule is part of the contract too: the first call has no previous
> reading, so its elapsed is zero. Getting that wrong shifts every subsequent
> value. Time is not special-cased — it is one more input to pin down.

---

# Case D: the bug that is not in the numbers

Values are comfortable. Real targets are not.

`pipeline_process` takes a config flag and calls three private helpers. The
helpers have different signatures, the parent takes a lock, and it writes into a
caller-owned buffer. And you cannot call the helpers: on the real target they are
not even exported.

So your reimplementation provides its own. The question becomes:

> Did my parent call the same helpers, in the same order, with the same
> arguments?

## Watch the call tree

The recorder hooks the parent *and* the three helpers, so the trace carries the
whole tree. Decoded, the parent's children come out as three distinct sequences:

```
config 0 -> [apply_gain, summarize]                        (2 children)
config 1 -> [apply_gain, normalize, summarize]             (3 children)
config 2 -> [apply_gain, normalize, apply_gain, summarize] (4 children)
```

**The paths are distinguishable by their call sequence alone**, before any value
comparison. Config 2 applies gain, normalizes, then gains again.

That sequence is the contract.

## The instrument

The replay provides the helpers as **shims**. Each checks itself against the
recorded contract before doing any arithmetic:

```cpp
void apply_gain(float* values, int count, float gain) {
    contract_child(310, 3, {identity_of(values), count, 0});
    for (int i = 0; i < count; ++i) values[i] *= gain;
}
```

`contract_child` verifies the kind, the arity, each argument, and that the parent
does not call more children than the recording has. Then it returns the recorded
result — so the arithmetic is yours, the *interaction* is the oracle's.

## The pointer problem

The first run fails strangely:

```
pipeline replay: child 1 arg 0: actual 1 != recorded 0
```

Argument zero is a buffer pointer. The oracle run had one address; yours has
another. **Comparing addresses across processes is meaningless.**

So pointers are recorded as **identity tokens**:

| Token | Buffer |
| --- | --- |
| 1 | the parent's private working copy |
| 2 | the caller's output buffer |
| 3 | the caller's input buffer |

The adapter derives these from the recording. The replay derives the same three
from its own run. Now the comparison is about *role*, not address.

> This is the part that surprises people. Normalization is not a detail added at
> the end; it is what makes the comparison possible. Every pointer becomes an
> identity, and the recorded graph becomes a shape your implementation must
> reproduce.

## The hunt

Inject the bug: on config 2, apply the gain **before** normalizing.

```
pipeline replay: child call identity or arity differs from the recording
```

Caught — not by a value, but by the **order of the calls**. Config 2 was
supposed to be `[gain, normalize, gain, summarize]`.

That is the class of bug Case A and B cannot see. Restore the order and the same
recording passes twelve cases on AArch64.

---

# Case E: the things the recording deliberately does not compare

Every PASS prints a `scope`:

```
"scope": "pipeline_process parent control flow, ordered child calls with
 arguments, returned count and the 4 output floats...; the lock and the global
 state object are not compared"
```

The recording **contains** the lock and the global state. The adapter still does
not compare them. That is the hardest judgement in the method.

The parent takes `pthread_mutex_lock`. A mutex is a **live platform object**: its
bytes differ run to run, and copying them across processes is nonsense. So:

- The lock is **really acquired and released** during replay, not synthesized.
- What is compared is the **ownership state** — held or not, at this boundary.
- The global state object is identified by role, like the buffers.

The line between "compare this" and "exclude this" cannot be drawn by a rule. It
is drawn by asking, for each field, *does this byte carry business meaning, or
platform implementation?* Allocator bookkeeping, reference counts, pthread
internals, absolute addresses: implementation. Buffer contents, call order, lock
ownership, returned values: business.

Get it wrong one way and the check is useless. Wrong the other way and it fails
forever on bytes that can never match. Every adapter in a real project ends up
with a `scope` string, because that string is the honest answer to "what did this
actually prove?"

---

# The part nobody tells you: your recording can be silently broken

Case C's recording worked on the first try. That is suspicious. A recording that
*lost an event* still parses, still has a call forest, and still produces a PASS —
for a smaller set of calls than you think.

The recorder refuses to produce such a file. Four checks, each catching a
different loss:

| Check | What it catches |
| --- | --- |
| **Batch sequence continuity** | A batch lost in transit (`1, 2, 4`) |
| **Per-thread event sequence** | An event dropped inside a batch (`1, 3` on one thread) |
| **Enter/leave correlation balance** | A leave with no enter — the first batch was lost |
| **Required-probe counters** | A required probe that never fired, so the file proves nothing about it |

The third one is why the agent does not start recording until the host says so:

```js
beginobservation() {
  acceptingEntries = true;   // called after script.load() returns
  return status();
}
```

The agent attaches as soon as the module is present. If it recorded during that
window, the first enter records would go nowhere and the file would contain
orphaned leaves. The writer detects that and **refuses to write the file at
all**:

```python
unbalanced = self._unbalanced_correlations()
if unbalanced:
    raise WriteError(f"{unbalanced} leave record(s) have no matching enter; "
                     "the first batch was lost...")
self.validate_sequence()
```

Why this matters more than it sounds: **a broken recording is worse than no
recording.** It produces a confident PASS over evidence you do not have. Every
check above exists because a plausible-looking trace once lied.

---

# What you have built

1. **records** a contract from a running native library, passively or by fixture,
2. **validates** the recording against the oracle's evidence, and itself,
3. **replays** your C++ on the actual device,
4. **compares** values, memory, call order, and lock ownership,
5. **normalizes** pointers to identities so two runs can be compared at all,
6. **captures the clock**, so a time-dependent function becomes reproducible,
7. **states its scope**, so a PASS cannot be mistaken for more than it is.

It never needed ABI compatibility, a shared header, or the original source.

---

# The rules, stated once

- **The recording is the oracle.** Not the disassembly, not the docs, not your
  intuition. If it is not in the recording, you cannot claim it.
- **Own the inputs.** A passive recording inherits its author's blind spots; a
  fixture chooses what to ask.
- **A fixture's answer is a claim.** Re-derive it from the trace or discard it.
- **Time is an input.** A function that reads a clock cannot be replayed until
  the readings are recorded and fed back.
- **How you interpret the recording is part of the contract.** The float/double
boundary is everywhere, including in your adapter. Match the oracle's
arithmetic, not just its result.
- **Pointers become identities.** An address is not evidence; a role is.
- **Compare business state, exclude platform state.** Write down which is which,
  every time.
- **A broken recording is worse than none.** Check sequence, balance, and
  required-probe counts before you trust a PASS.

---

# Running it yourself

Requirements: Python with the `frida` package **matching the device's
`frida-server`**, an Android device with root and a running `frida-server`, and
the NDK toolchain.

```bash
# 1. Build the originals
pwsh -File tools/build-example.ps1

# 2. A driver, for the passive cases
adb push build/libgeom.so build/geom-driver /data/local/tmp/
adb shell chmod 755 /data/local/tmp/geom-driver
adb shell "nohup /data/local/tmp/geom-driver >/dev/null 2>&1 &"

# 3. Passive record, then compare
python3 -m rrfrida.record --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin
python3 tutorial/compare_geom.py build/demo.bin --serial <serial> --compiler "$NDK/clang++.exe"

# 4. Active fixture record, then compare
python3 -m rrfrida.fixture --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --fixture tutorial/example/fixture_geom.js --output build/fixture.bin
python3 tutorial/compare_fixture.py build/fixture.bin --serial <serial> --compiler "$NDK/clang++.exe"

# 5. Time-dependent function, with the logical clock recorded
adb push build/libpipeline.so build/pipeline-driver /data/local/tmp/
adb shell chmod 755 /data/local/tmp/pipeline-driver
adb shell "nohup /data/local/tmp/pipeline-driver >/dev/null 2>&1 &"
python3 -m rrfrida.fixture --serial <serial> --process pipeline-driver \
    --probe-set tutorial/example/predict-probe-set.json --probes-dir probes \
    --fixture tutorial/example/fixture_predict.js --capture-clock \
    --output build/predict.bin
python3 tutorial/compare_predict.py build/predict.bin --serial <serial> --compiler "$NDK/clang++.exe"
```

The parent-contract pipeline case is registered in `cases.json`; run it with
`python3 -m rrfrida.run pipeline --trace build/pipeline.bin ...`.

> Restart the target process between recordings of a stateful function. Static
> state persists across fixture runs in the same process, so a second recording
> in a live process starts from the first one's leftovers — as the first attempt
> at `pipeline_predict` showed, returning a non-zero delta where the fresh
> process returned zero.

---

# Appendix: the operational details that cost real time

## Registers are not arguments

AArch64 passes floating-point arguments in `s0..s3` and integers/pointers in
`x0..x7`. Frida's `args[]` array is the **X bank only**. Two consequences that
crash a recorder if you forget them:

- `geom_scale(float a, float b, float* out)` has `out` in **`arg0`**, not `arg2`.
  Reading `arg2` dereferences leftover integer garbage.
- A float argument must be a **register snapshot**: `{ "phase": "enter",
  "register": "s0" }`, not `{ "source": "arg0" }`.

Frida reports an S register **by value**: `s0` for `2.0f` comes back as the
number `2`, not `0x40000000`. The agent re-encodes FP registers as IEEE-754 bits
so snapshots are byte-comparable.

## A pointer and the data at that pointer are two captures

`{ "source": "arg1", "size": 8 }` reads eight bytes **at** the address in `arg1`.
To record the address itself, use `{ "register": "x1" }`. Confusing the two is
how the first pointer comparison returned a packed pair of floats instead of an
address.

For a buffer whose length is an argument, use a dynamic snapshot:

```json
{ "phase": "enter", "source": "arg1", "size_arg": 2, "multiplier": 4, "max": 256 }
```

That reads `arg2 * 4` bytes at `arg1`, capped at 256. Dynamic snapshots must be
captured on enter, where the arguments are known.

## Optional captures

A probe can ask for a pointer that is null on some path. Mark it `optional` and a
failed capture records null instead of aborting the agent:

```json
{ "phase": "leave", "source": "arg3", "size": 16, "optional": true }
```

The reader reports `captured: false`, and the adapter decides whether that is
acceptable. A zero-filled failed read is never treated as observed memory.

## The logical clock

Recording a clock means three things, in order:

1. **Capture the readings the target took.** Hook `clock_gettime`, keep only
   `CLOCK_MONOTONIC`, only successes, and only callers whose return address is
   inside the target module. Record `(seconds, nanos)` per call in order.
2. **Interpret them exactly as the oracle did.** For `pipeline_predict` that
   means narrowing the nanosecond difference to `float` before dividing, and
   treating the first call as zero elapsed. Both rules are part of the contract.
3. **Feed them back.** The reimplementation asks for the elapsed value; the
   replay supplies it from the recording. The reimplementation never reads a
   clock.

Two traps:

- Virtualizing the clock for the whole process hangs the host. The harness and
  the Android UI share the address space and depend on real timeouts. Filter by
  caller module.
- A stateful function carries state across recordings in a live process. If the
  first recorded call does not return what a fresh process would, restart the
  target before recording.

## Common failures

| Symptom | Cause |
| --- | --- |
| `unable to connect to remote frida-server` | No `frida-server`, or client/server version mismatch |
| `instruction mismatch at 0x...` | Probes point at another build; regenerate with `make_probes.py` |
| Agent `access violation` on a snapshot | A snapshot dereferenced a non-pointer (float or integer argument) |
| `unknown register w0` | Frida exposes `x0`, not `w0`; mask to 32 bits in the adapter |
| `leave without matching enter` | The first batch was lost, or a probe hooked a mid-call address |
| `thread 0x... sequence N, expected M` | An event was dropped inside a batch |
| `required probe kind N never fired` | The recording proves nothing about that probe |
| `fixture claim N disagrees` | The fixture misread memory; the trace is the truth |
| `the recording has no clock readings` | Record a time-dependent function with `--capture-clock` |
| A time-dependent result differs by one byte | The adapter's elapsed arithmetic is not the oracle's |
| `child call identity or arity differs` | Your parent's control flow diverges |
| `child call argument differs` | Same calls, different arguments, or an un-normalized pointer |
| First diff in an output field | Your arithmetic differs; the tool is working |
