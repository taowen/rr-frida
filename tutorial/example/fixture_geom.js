'use strict';
// A fixture that calls the official geom functions directly.
//
// Unlike a passive recording, this does not wait for a driver to call the
// library. It allocates its own output buffer, calls the entry through a
// `NativeFunction`, and reads the results. The inputs are chosen here, so the
// recording covers cases the author wants -- including ones that expose a
// difference a passive recording might never reach.
//
// The agent binds the regions below and records only calls made from this
// thread, so the trace is exactly this fixture's work.

const SCALE_RVA = 0x45f8;
const DIRECTION_RVA = 0x4610;

let module, output, tid;

rpc.exports = {
  setup() {
    module = Process.getModuleByName('libgeom.so');
    tid = Process.getCurrentThreadId();
    // One 16-byte output buffer, reused across cases. Each case overwrites it.
    output = Memory.alloc(16);
    return {tid, regions: {output: {address: output.toString(), size: 16}}};
  },

  run() {
    const scale = new NativeFunction(module.base.add(SCALE_RVA), 'float',
                                     ['float', 'float', 'pointer']);
    const direction = new NativeFunction(module.base.add(DIRECTION_RVA), 'void',
                                         ['float', 'float', 'float', 'pointer']);
    const results = [];
    // (1, 2, 3) is the input where a float length and a double length round
    // differently, so this recording can distinguish the two implementations.
    const directionCases = [
      [1.0, 2.0, 3.0],
      [1.0, 2.0, 2.0],
      [-1.0, -2.0, -3.0],
      [1.0e-4, 2.0e-4, 3.0e-4],
    ];
    for (const [x, y, z] of directionCases) {
      direction(x, y, z, output);
      results.push({
        kind: 'direction',
        input: [x, y, z],
        output: [output.readFloat(), output.add(4).readFloat(), output.add(8).readFloat()],
      });
    }
    const scaleCases = [[2.0, 3.0], [1.0000001, 1.0000001], [-1.0, 0.5]];
    for (const [a, b] of scaleCases) {
      const returned = scale(a, b, output);
      results.push({
        kind: 'scale',
        input: [a, b],
        output: [output.readFloat(), output.add(4).readFloat()],
        returned: returned,
      });
    }
    return {tid, cases: results.length, results};
  },
};
