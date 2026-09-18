'use strict';
// rr-frida recorder agent.
//
// Attaches to one module and records the declared probes as a binary event
// stream. It records *contracts*, not execution: for each hooked function it
// stores the arguments, the return value, ordered child calls, and whatever
// memory snapshots the probe declares.
//
// Design rules, learned from real use:
//
//  * Verify the module identity and every probe's instruction bytes before
//    attaching. A recording produced from a different build is worse than no
//    recording, because it looks valid.
//  * One native listener per address. Multiple native listeners at one entry
//    have silently lost onLeave records; logical probes fan out from a single
//    listener instead.
//  * Keep the per-invocation state on an explicit per-thread stack. Frida does
//    not reliably retain custom properties on the invocation context when an
//    instruction interceptor fires inside the hooked function.
//  * Batch events per thread and flush on a size boundary. Sending per event
//    perturbs the target and loses records under load.

const config = globalThis.RRFridaConfig;
const layouts = globalThis.RRFridaLayouts;
const catalog = globalThis.RRFridaProbeCatalog;

const counters = {};
const threadSequences = new Map();
const timeBuffers = new Map();
const listeners = [];
const threadBatches = new Map();
let installed = false;
let installing = false;
let moduleInfo = null;
let eventCount = 0;
let dropCount = 0;
let correlation = 0n;
let batchSequence = 0;
// Recording starts disabled and is enabled by the recorder over RPC. The agent
// attaches as soon as the module is present, which can be before the host is
// ready to receive batches; recording during that window loses the first
// enter records and leaves orphaned leaves in the file.
let acceptingEntries = false;

const clockGettime = new NativeFunction(
    Module.getExportByName('libc.so', 'clock_gettime'), 'int', ['int', 'pointer']);

function currentMonotonicNs(tid) {
  let storage = timeBuffers.get(tid);
  if (storage === undefined) {
    storage = Memory.alloc(16);
    timeBuffers.set(tid, storage);
  }
  // CLOCK_MONOTONIC == 1. Timestamps order events; they are not replayed.
  if (clockGettime(1, storage) !== 0) throw new Error('clock_gettime failed');
  return BigInt(storage.readS64().toString()) * 1000000000n
      + BigInt(storage.add(8).readS64().toString());
}

function asBigInt(value) {
  if (value === null || value === undefined) return 0n;
  return BigInt(value.toString());
}

function registerBytes(name, value) {
  // Frida exposes integer registers as integer values, but exposes FP
  // registers by their numeric value. `s0` for 2.0f gives the number 2, not the
  // bit pattern 0x40000000, and the u64 form below would truncate it. Encode
  // FP registers as their IEEE-754 bits so snapshots are comparable byte for
  // byte, which is the only form the replayer can use.
  const buffer = new ArrayBuffer(8);
  const view = new DataView(buffer);
  if (name[0] === 's') {
    view.setFloat32(0, value, true);
  } else if (name[0] === 'd') {
    view.setFloat64(0, value, true);
  } else {
    view.setBigUint64(0, asBigInt(value), true);
  }
  return new Uint8Array(buffer);
}

function newBatch() {
  const buffer = new ArrayBuffer(layouts.batchBytes);
  return {buffer, view: new DataView(buffer), offset: 0, records: 0};
}

function batchForThread(tid) {
  let state = threadBatches.get(tid);
  if (state === undefined) {
    state = newBatch();
    threadBatches.set(tid, state);
  }
  return state;
}

function flushBatch(tid, state) {
  if (state.offset === 0) return;
  const data = state.buffer.slice(0, state.offset);
  const bytes = state.offset;
  const records = state.records;
  // Replace the buffer before send(): host message delivery can let callbacks
  // from another native thread enter the agent while send() is in flight.
  threadBatches.set(tid, newBatch());
  try {
    send({type: 'batch', batch_sequence: ++batchSequence, bytes, records}, data);
  } catch (error) {
    dropCount += records;
    send({type: 'agent_error', error: String(error)});
  }
}

function flushAllBatches() {
  for (const [tid, state] of Array.from(threadBatches.entries())) {
    flushBatch(tid, state);
  }
}

function writeU64(view, offset, value) {
  view.setBigUint64(offset, asBigInt(value), true);
}

function recordEvent(kind, phase, tid, correlationId, values, snapshot) {
  const raw = snapshot?.bytes ?? new Uint8Array(0);
  const unaligned = layouts.registerPayloadBytes + raw.byteLength;
  const totalSize = (layouts.eventHeaderBytes + unaligned + 7) & ~7;
  const payloadSize = totalSize - layouts.eventHeaderBytes;
  try {
    if (totalSize > layouts.batchBytes) throw new Error('event exceeds batch size');
    const timestamp = currentMonotonicNs(tid);
    const sequence = (threadSequences.get(tid) ?? 0) + 1;
    threadSequences.set(tid, sequence);
    let state = batchForThread(tid);
    if (state.offset + totalSize > state.buffer.byteLength) {
      flushBatch(tid, state);
      state = batchForThread(tid);
    }
    const start = state.offset;
    const view = state.view;
    view.setUint32(start, totalSize, true);
    view.setUint16(start + 4, layouts.eventHeaderBytes, true);
    view.setUint16(start + 6, kind, true);
    view.setUint8(start + 8, phase);
    view.setUint8(start + 9, snapshot?.flags ?? 0);
    view.setUint16(start + 10, payloadSize, true);
    view.setUint32(start + 12, tid, true);
    view.setUint32(start + 16, sequence, true);
    writeU64(view, start + 20, timestamp);
    writeU64(view, start + 28, correlationId);
    view.setUint32(start + 36, 0, true);
    for (let index = 0; index < 5; index++) {
      writeU64(view, start + layouts.eventHeaderBytes + index * 8, values[index]);
    }
    new Uint8Array(state.buffer, start + layouts.eventHeaderBytes
        + layouts.registerPayloadBytes, raw.byteLength).set(raw);
    new Uint8Array(state.buffer, start + layouts.eventHeaderBytes
        + layouts.registerPayloadBytes + raw.byteLength,
        payloadSize - unaligned).fill(0);
    state.offset += totalSize;
    state.records++;
    eventCount++;
  } catch (error) {
    dropCount++;
    send({type: 'agent_error', error: String(error), kind, phase, tid});
  }
}

function resolveSnapshotBase(source, args, context, invocation) {
  if (source.startsWith('entry_')) {
    const register = source.substring(6);
    if (args !== null) {
      const value = context[register];
      invocation[source] = value;
      return value;
    }
    return invocation[source];
  }
  if (source.startsWith('arg')) {
    const index = Number(source.substring(3));
    if (args !== null) {
      const value = args[index];
      invocation[source] = value;
      return value;
    }
    return invocation[source];
  }
  if (source === 'sp') return context.sp;
  if (source === 'fp') return context.fp;
  return context[source];
}

function captureSnapshots(definition, phase, args, context, invocation) {
  // Retain entry addresses even when the buffer is captured only on return.
  // Do not read uninitialized output memory just to keep a pointer.
  if (phase === 'enter') {
    for (const snapshot of definition.snapshots ?? []) {
      if (snapshot.phase === 'leave' && snapshot.source !== undefined
          && (snapshot.source.startsWith('arg') || snapshot.source.startsWith('entry_'))) {
        resolveSnapshotBase(snapshot.source, args, context, invocation);
      }
    }
  }
  const selected = (definition.snapshots ?? []).filter(s => s.phase === phase);
  if (selected.length === 0) return {bytes: new Uint8Array(0), flags: 0};
  const chunks = [];
  let total = 0;
  let flags = 0;
  for (let index = 0; index < selected.length; index++) {
    const snapshot = selected[index];
    let size = snapshot.size ?? 0;
    // A dynamic snapshot reads `args[size_arg] * multiplier` bytes, capped at
    // `max`. This is how a pointer argument with a runtime length is captured
    // without knowing the length in advance.
    if (snapshot.size_arg !== undefined) {
      if (args === null) throw new Error('dynamic snapshot needs call arguments');
      const count = args[snapshot.size_arg].toUInt32();
      const multiplier = snapshot.multiplier ?? 1;
      size = Math.min(snapshot.max ?? count * multiplier, count * multiplier);
      if (!Number.isSafeInteger(size) || size < 0) {
        throw new Error('invalid dynamic snapshot size');
      }
    }
    try {
      if (snapshot.register !== undefined) {
        const value = context[snapshot.register];
        if (value === undefined) throw new Error('unknown register ' + snapshot.register);
        const bytes = registerBytes(snapshot.register, value);
        chunks.push(bytes);
        total += bytes.byteLength;
      } else {
        let base = resolveSnapshotBase(snapshot.source, args, context, invocation);
        for (const pointerOffset of snapshot.pointer_offsets ?? []) {
          base = base.add(pointerOffset).readPointer();
          if (base.isNull()) throw new Error('null pointer-chain snapshot base');
        }
        if (base === undefined || base.isNull()) throw new Error('null snapshot base');
        chunks.push(new Uint8Array(
            base.add(snapshot.offset ?? 0).readByteArray(size)));
        total += size;
      }
      flags |= 1 << index;
    } catch (error) {
      if (!snapshot.optional) throw error;
      chunks.push(new Uint8Array(size));
      total += size;
    }
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return {bytes: result, flags};
}

function passesFilter(definition, args) {
  // Named predicates keep the agent small. Add one only when a recording
  // genuinely needs to select among calls at the same address.
  return true;
}

function functionCallbacks(name, definition, moduleBase) {
  const callerReturns = definition.callerReturnRvas === undefined ? null
      : new Set(definition.callerReturnRvas.map(rva => moduleBase.add(rva).toString()));
  counters[name] = {enter: 0, leave: 0};
  // Per-probe, per-thread invocation stack. Models recursion and avoids
  // relying on context properties that Frida may not retain.
  const invocations = new Map();
  return {
    onEnter(args) {
      const tid = Process.getCurrentThreadId();
      let stack = invocations.get(tid);
      if (stack === undefined) {
        stack = [];
        invocations.set(tid, stack);
      }
      if (!acceptingEntries
          || !passesFilter(definition, args)
          || (callerReturns !== null && !callerReturns.has(this.context.lr.toString()))) {
        stack.push({skip: true});
        return;
      }
      const id = ++correlation;
      const invocation = {};
      stack.push({skip: false, tid, id, invocation});
      counters[name].enter++;
      const snapshot = captureSnapshots(definition, 'enter', args, this.context, invocation);
      recordEvent(definition.kind, layouts.phaseEnter, tid, id,
          [args[0], args[1], args[2], args[3], this.context.lr], snapshot);
    },
    onLeave(retval) {
      const tid = Process.getCurrentThreadId();
      const stack = invocations.get(tid);
      if (stack === undefined || stack.length === 0) {
        throw new Error(name + ' leave without matching entry');
      }
      const state = stack.pop();
      if (stack.length === 0) invocations.delete(tid);
      if (state.skip) return;
      counters[name].leave++;
      const snapshot = captureSnapshots(definition, 'leave', null, this.context, state.invocation);
      recordEvent(definition.kind, layouts.phaseLeave, state.tid, state.id,
          [retval, 0, 0, 0, this.context.lr], snapshot);
    },
  };
}

function installFunctionGroup(probes, address, moduleBase) {
  // One native listener fans out to every logical probe at this address.
  // Reverse the exits so nested correlations unwind in order.
  const callbacks = probes.map(([name, definition]) =>
      functionCallbacks(name, definition, moduleBase));
  return Interceptor.attach(address, {
    onEnter(args) {
      for (const callback of callbacks) callback.onEnter.call(this, args);
    },
    onLeave(retval) {
      for (let index = callbacks.length - 1; index >= 0; --index) {
        callbacks[index].onLeave.call(this, retval);
      }
    },
  });
}

function installInstruction(name, definition, address) {
  counters[name] = {hit: 0};
  return Interceptor.attach(address, {
    onEnter() {
      if (!acceptingEntries) return;
      const tid = Process.getCurrentThreadId();
      counters[name].hit++;
      const snapshot = captureSnapshots(definition, 'instruction', null, this.context, {});
      recordEvent(definition.kind, layouts.phaseInstruction, tid, 0,
          [this.context.x0, this.context.x1, this.context.x2,
            this.context.x3, this.context.lr], snapshot);
    },
  });
}

function status() {
  return {
    installed,
    module: moduleInfo,
    counters,
    event_count: eventCount,
    drop_count: dropCount,
    frida_version: typeof Frida !== 'undefined' ? Frida.version : 'unknown',
    arch: Process.arch,
    pointer_size: Process.pointerSize,
    probe_set: config.probeSet.name,
    session_id: config.sessionId,
  };
}

function install(module) {
  if (installed || installing) return;
  installing = true;
  try {
    // Validate identity and instruction bytes before attaching anything.
    const identity = verifyModuleIdentity(module, config.probeSet);
    const validated = [];
    for (const name of [...config.probeSet.required, ...(config.probeSet.optional ?? [])]) {
      const definition = catalog[name];
      if (definition === undefined) throw new Error('unknown probe ' + name);
      let address;
      if (definition.type === 'got') {
        address = module.base.add(definition.rva).readPointer();
        if (address.isNull() || Process.findModuleByAddress(address) === null) {
          throw new Error(name + ' GOT target is not mapped executable code');
        }
      } else {
        address = module.base.add(definition.rva);
        const actual = byteHex(address, definition.expected.length / 2);
        if (actual !== definition.expected) {
          throw new Error(name + ' instruction mismatch at 0x'
              + definition.rva.toString(16) + ': expected '
              + definition.expected + ', got ' + actual);
        }
      }
      validated.push([name, definition, address]);
    }
    const functionGroups = new Map();
    for (const [name, definition, address] of validated) {
      if (definition.type === 'function' || definition.type === 'got') {
        const key = address.toString();
        if (!functionGroups.has(key)) functionGroups.set(key, {address, probes: []});
        functionGroups.get(key).probes.push([name, definition]);
      } else {
        listeners.push(installInstruction(name, definition, address));
      }
    }
    for (const {address, probes} of functionGroups.values()) {
      listeners.push(installFunctionGroup(probes, address, module.base));
    }
    moduleInfo = {
      name: module.name,
      base: module.base.toString(),
      size: module.size,
      ...identity,
      probes: validated.map(([name, definition, address]) => ({
        name,
        kind: definition.kind,
        type: definition.type,
        rva: definition.rva,
        ...(definition.expected === undefined ? {} : {expected: definition.expected}),
        resolved_address: address.toString(),
        required: config.probeSet.required.includes(name),
        snapshots: definition.snapshots ?? [],
        callerReturnRvas: definition.callerReturnRvas ?? null,
      })),
    };
    installed = true;
    send({type: 'installed', status: status()});
  } catch (error) {
    send({type: 'install_error', error: String(error)});
  } finally {
    installing = false;
  }
}

rpc.exports = {
  beginobservation() {
    // Start recording. Called by the recorder after script.load() returns, so
    // no event is produced before the host can receive it.
    counters; // ensure defined
    acceptingEntries = true;
    return status();
  },
  flush() {
    flushAllBatches();
    return status();
  },
  async finishobservation() {
    // Stop accepting new entries, then let in-flight calls return.
    //
    // The wait must yield. A busy spin on Frida's JS thread can starve the
    // interceptor callbacks that are supposed to deliver the pending leaves,
    // which then trips the "unfinished calls" guard on a recording that was
    // fine. Poll with a small sleep and a generous deadline.
    acceptingEntries = false;
    const deadline = Date.now() + 5000;
    const unfinished = () =>
        Object.values(counters).some(v => v.enter !== undefined && v.enter !== v.leave);
    while (unfinished()) {
      if (Date.now() >= deadline) throw new Error('observation has unfinished calls');
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    flushAllBatches();
    return status();
  },
  status() {
    return status();
  },
};

const loaded = Process.findModuleByName(config.probeSet.module);
if (loaded !== null) {
  install(loaded);
} else {
  send({type: 'waiting', module: config.probeSet.module});
  Process.attachModuleObserver({
    onAdded(module) {
      if (module.name === config.probeSet.module) install(module);
    },
  });
}
