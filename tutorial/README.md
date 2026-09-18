# The reimplementation that passed every test and was still wrong

You are given a stripped Android `.so`. One function in it, `pipeline_process`,
is the entry point your product calls. You have to rewrite it in your own C++.

You cannot call the library. You cannot link it. You have the decompiled body and
three helper functions it calls, all private. Your job is a new implementation
that behaves identically.

You write it. It compiles. It returns the right numbers on every input you try.

And it is wrong.

This tutorial is the hunt for that bug, and for the two smaller ones hidden
backstage. You will build a recording of the original, replay it against your
code, and watch it produce the first differing byte. Then you will fix it. Then
you will discover the class of bug the first method *cannot* catch, and build the
instrument that can.

Everything here runs on a real device. Nothing is a thought experiment.

---

## Cast of characters

| Piece | What it is |
| --- | --- |
| `libgeom.so` | The original library. Two leaf functions, for warm-up. |
| `libpipeline.so` | The original library with the parent function and its three private helpers. |
| `driver.cpp` | A process that calls the library in a loop, so a recorder has something to watch. |
| `mine.cpp` | Your reimplementation of the leaf functions. |
| `pipeline_mine.cpp` | Your reimplementation of the parent. |
| `probes/*.json` | Declarations of *what to watch*: which functions, which arguments, which memory. |
| `rr-frida` | The recorder, the replayer, and the comparators. |

The rule that makes all of this possible: **a recording stores a contract, not an
execution**. It records what was called and what the world looked like, not the
instructions. So the thing you record from does not need to resemble the thing
you replay into.

---

# Case A: the last bit that nobody sees

## The setup

Build the first library, a driver that calls it, and the recording probes:

```bash
pwsh -File tools/build-example.ps1          # builds libgeom.so and geom-driver
python3 tools/make_probes.py build/libgeom.so --nm "$NDK/llvm-nm.exe" ... \
    --output probes/geom.json
```

`geom_direction(float x, float y, float z, float* out)` normalizes a vector. Your
`mine.cpp` does the same thing, except it computes the length in `double` and
narrows at the end. The original computes it in `float`.

You try it on `(1, 2, 2)`. Your output matches. You try a few more. They match.
You ship.

## The evidence

Record the original from a running process on the device:

```bash
python3 -m rrfrida.record --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin
```

Replay your code against the recording:

```bash
python3 tutorial/compare_geom.py build/demo.bin --serial <serial> --compiler "$NDK/clang++.exe"
```

```
rrtrace.format.TraceError: direction[0] byte 8: actual 0xb3 != official 0xb2
```

**One byte. The last byte of the third output component.** The original produced
`0x3f4d...b2`, you produced `0x3f4d...b3`. One unit in the last place.

## What just happened

Your test inputs never exercised the difference. `(1, 2, 2)` happens to round the
same in `float` and `double`. The recording used `(1, 2, 3)`, which does not. You
did not choose that input — the original's driver did, and the recording carried
it forward.

This is the whole argument for the method, in one byte: **a recording is evidence
of what the original actually did, including the cases your intuition skipped.**

Fix the length to `float` and the same recording passes. Nothing about the
recording changed; only your code did.

> You can reproduce the counterexample on the host, without a device:
> `tutorial/README.md` notes `(1,2,3)` is a case where `float` and `double`
> lengths differ. Choosing an input that *can* expose the difference is part of
> the job. A recording proves what it covers.

---

# Case B: the bug that is not in the numbers

Case A is comfortable. Values go in, values come out, you compare them. Real
targets are not so kind.

`pipeline_process` takes a config flag and calls three private helpers. The
helpers have different signatures, the parent takes a lock, and it writes into a
caller-owned buffer, not a return value. And here is the part that breaks the
value-comparison approach: **you cannot call the helpers**. They are not exported.
On the real target you would not even have their names.

So your reimplementation has to provide its own helpers. Which means the question
is no longer "did the numbers match". It is:

> Did my parent call the same helpers, in the same order, with the same
> arguments?

## Watch what the recording sees

Record the parent. The recorder hooks the parent *and* the three helpers, so the
trace carries the whole call tree. Look at it:

```bash
python3 -m rrfrida.inspect build/pipeline.bin --limit 6 --snapshots
```

Decoded, the parent's children come out as three distinct sequences:

```
config 0 -> [apply_gain, summarize]                    (2 children)
config 1 -> [apply_gain, normalize, summarize]         (3 children)
config 2 -> [apply_gain, normalize, apply_gain, summarize]   (4 children)
```

**The paths are distinguishable by their call sequence alone**, before any value
comparison. Config 2 applies gain, normalizes, then applies gain again. Config 1
normalizes after the first gain. Config 0 never normalizes.

That sequence is the contract.

## The instrument

The reimplementation calls helpers through declarations the replay provides:

```cpp
namespace tutorial_helpers {
void apply_gain(float* values, int count, float gain);
float normalize(float* values, int count);
int summarize(const float* values, int count, float scale, float* out);
}
```

The replay defines these as **shims**. Each shim checks itself against the
recorded contract before doing any arithmetic:

```cpp
void apply_gain(float* values, int count, float gain) {
    contract_child(310, 3, {identity_of(values), count, 0});
    for (int i = 0; i < count; ++i) values[i] *= gain;
}
```

`contract_child` verifies, against the next recorded child:

1. the **kind** (which helper),
2. the **arity** (how many arguments),
3. each **argument**, and
4. that the parent does not call more children than the recording has.

Then it returns the recorded result, so the shim's arithmetic is yours while the
*interaction* is the original's.

## The pointer problem, and why identity is not address

The first run of this produces a strange failure:

```
pipeline replay: child 1 arg 0: actual 1 != recorded 0
```

Argument zero is the buffer pointer. In the official run it was one address; in
your run it is another. **Comparing addresses across processes is meaningless.**
Every run places the heap and stack somewhere else.

So arguments that are pointers are recorded as **identity tokens**:

| Token | Buffer |
| --- | --- |
| 1 | the parent's private working copy |
| 2 | the caller's output buffer |
| 3 | the caller's input buffer |

The adapter derives these from the recording (input is `arg1`, output is `arg3`,
and the working copy is whatever the parent hands to its first child). The replay
derives the same three from its own run. Now the comparison is about *role*, not
address, and it is stable.

> This is the part that surprises people. Normalization is not a detail you add at
> the end; it is what makes the comparison possible at all. The same idea scales
> to object graphs: every pointer becomes an identity, and the recorded graph
> becomes a shape your implementation must reproduce.

## The hunt

Now inject the bug. On config 2, apply the gain **before** normalizing:

```cpp
// wrong order
apply_gain(work, count, 2.0f);
const float peak = normalize(work, count);
```

Run the comparison:

```
pipeline replay: child call identity or arity differs from the recording
```

Caught. Not by a value — the outputs might even coincide — but by the **order of
the calls**. Config 2 was supposed to be `[gain, normalize, gain, summarize]`, and
your version produced `[gain, gain, normalize, summarize]`.

That is the class of bug Case A's method cannot see. And it is the reason this
project exists.

## Put it back

Restore the correct order and the same recording passes:

```json
{ "result": "PASS", "cases": 12, ... }
```

Twelve cases across three config paths, each checked for returned count, output
values, and the ordered child contract with arguments. On AArch64.

---

# Case C: the things the recording deliberately does not compare

Look closely at the `scope` field every PASS prints:

```
"scope": "pipeline_process parent control flow, ordered child calls with
 arguments, returned count and the 4 output floats, for the three recorded
 config paths; the lock and the global state object are not compared"
```

The recording **contains** the lock and the global state. The adapter still does
not compare them. That is not an oversight; it is the hardest judgement in the
whole method.

The parent takes `pthread_mutex_lock`. The recording captured that call and the
mutex's held/not-held state before and after. But a mutex is a **live platform
object**. Its bytes differ run to run, and copying them across processes produces
nonsense. So:

- The lock is **really acquired and released** during replay, not synthesized.
- What is compared is the **ownership state** — was the lock held at this
  boundary, yes or no — never the mutex's internal bytes.
- The global state object is identified by role, like the buffers, never by
  address.

The line between "compare this" and "exclude this" cannot be drawn by a rule. It
is drawn by asking, for each field, *does this byte carry business meaning, or
platform implementation?* Allocator bookkeeping, reference counts, pthread
internals, and absolute addresses are implementation. Buffer contents, call
order, lock ownership, and returned values are business.

Get it wrong in one direction and the check is useless — it passes everything.
Get it wrong the other way and it fails forever on bytes that can never match.
Every adapter in a real project ends up with a `scope` string like the one above,
because that string is the honest answer to "what did this actually prove?"

---

# What you have built

By the end you have a pipeline that:

1. **records** a contract from a running native library with Frida,
2. **validates** the recording against the official evidence before trusting it,
3. **replays** your C++ on the actual device,
4. **compares** values, memory, call order, and lock ownership, and
5. **states its scope**, so a PASS cannot be mistaken for more than it is.

It never needed ABI compatibility, a shared header, or access to the original
source. It needed evidence of what the original did, and a way to make your run
speak the same language.

---

# The rules, stated once

- **The recording is the oracle.** Not the disassembly, not the docs, not your
  intuition. If it is not in the recording, you cannot claim it.
- **Pointers become identities.** An address is not evidence; a role is.
- **Compare business state, exclude platform state.** And write down which is
  which, every time.
- **A PASS has a scope.** The scope is part of the result, not a footnote.
- **Choose inputs that can fail.** A recording of cases that cannot distinguish
  two implementations proves nothing about them.

---

# Running it yourself

Requirements: Python with the `frida` package matching the device's
`frida-server`, an Android device with root and a running `frida-server`, and the
NDK toolchain.

```bash
# 1. Build the originals and a driver
pwsh -File tools/build-example.ps1

# 2. Push and run the driver
adb push build/libgeom.so build/geom-driver /data/local/tmp/
adb shell chmod 755 /data/local/tmp/geom-driver
adb shell "nohup /data/local/tmp/geom-driver >/dev/null 2>&1 &"

# 3. Record, inspect, compare
python3 -m rrfrida.record --serial <serial> --process geom-driver \
    --probe-set tutorial/example/probe-set.json --probes-dir probes \
    --duration 2 --output build/demo.bin
python3 -m rrfrida.inspect build/demo.bin --snapshots --limit 1
python3 tutorial/compare_geom.py build/demo.bin --serial <serial> --compiler "$NDK/clang++.exe"
```

The full step-by-step, including the probe-declaration traps that cost real time
(float arguments live in the S registers, not `args[]`; integer arguments are not
pointers), is in the sections below.

---

# Appendix: the operational details

## Registers are not arguments

AArch64 passes floating-point arguments in `s0..s3` and integers/pointers in
`x0..x7`. Frida's `args[]` array is the **X bank only**. Two consequences that
crash a recorder if you forget them:

- `geom_scale(float a, float b, float* out)` has `out` in **`arg0`**, not `arg2`.
  Reading `arg2` dereferences leftover integer garbage.
- A float argument must be captured as a **register snapshot**, not a memory
  snapshot. `{ "phase": "enter", "register": "s0" }`, not `{ "source": "arg0" }`.

And Frida reports an S register **by value**: `s0` for `2.0f` comes back as the
number `2`, not the bit pattern `0x40000000`. The agent re-encodes FP registers
as IEEE-754 bits so snapshots are byte-comparable.

## A pointer and the data at that pointer are two captures

`{ "source": "arg1", "size": 8 }` reads eight bytes **at** the address in
`arg1`. To record the address itself, use `{ "register": "x1" }`. Confusing the
two is how the first pointer comparison returned a packed pair of floats instead
of an address.

For a buffer whose length is an argument, use a dynamic snapshot:

```json
{ "phase": "enter", "source": "arg1", "size_arg": 2, "multiplier": 4, "max": 256 }
```

That reads `arg2 * 4` bytes at `arg1`, capped at 256. Dynamic snapshots must be
captured on enter, where the arguments are known.

## Optional captures

A probe can ask for a pointer that is null on some path. Mark the snapshot
`optional` and a failed capture records null instead of aborting the agent:

```json
{ "phase": "leave", "source": "arg3", "size": 16, "optional": true }
```

The reader then reports `captured: false` for that field, and the adapter decides
whether that is acceptable. It never treats a zero-filled failed read as observed
memory.

## The recorder is not recording until you say so

The agent attaches as soon as the module is present, which can be before the host
is receiving messages. Recording starts disabled and is enabled over RPC after
`script.load()` returns. Without that handshake the first batch is lost and the
file contains leaves with no matching enters. The writer refuses to produce such
a file.

## Common failures

| Symptom | Cause |
| --- | --- |
| `unable to connect to remote frida-server` | No `frida-server`, or client/server version mismatch |
| `instruction mismatch at 0x...` | Probes point at another build; regenerate with `make_probes.py` |
| Agent `access violation` on a snapshot | A snapshot dereferenced a non-pointer (float or integer argument) |
| `unknown register w0` | Frida exposes `x0`, not `w0`; mask to 32 bits in the adapter |
| `leave without matching enter` | The first batch was lost, or a probe hooked a mid-call address |
| `child call identity or arity differs` | Your parent's control flow diverges from the original |
| `child call argument differs` | Same calls, different arguments, or an un-normalized pointer |
| First diff in an output field | Your arithmetic differs; the tool is working |
