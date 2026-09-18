'use strict';
// A fixture for the time-dependent function.
//
// `pipeline_predict` reads CLOCK_MONOTONIC. Its result depends on the time
// between calls, so two runs of this fixture produce different numbers unless
// the clock readings are recorded.
//
// The fixture deliberately sleeps between calls, so the elapsed time is large
// enough that "the wall clock changed" is unmistakable in the output.

const PREDICT_RVA = 0x4b6c;
const INPUT = [1.0, -2.0, 3.0, -4.0];

let module, output, tid;

rpc.exports = {
  setup() {
    module = Process.getModuleByName('libpipeline.so');
    tid = Process.getCurrentThreadId();
    output = Memory.alloc(8);
    return {tid, regions: {output: {address: output.toString(), size: 8}}};
  },

  run() {
    const predict = new NativeFunction(module.base.add(PREDICT_RVA), 'float',
                                       ['pointer', 'int', 'pointer']);
    const input = Memory.alloc(4 * INPUT.length);
    for (let i = 0; i < INPUT.length; ++i) {
      input.add(4 * i).writeFloat(INPUT[i]);
    }

    const results = [];
    const record = (label) => {
      const returned = predict(input, INPUT.length, output);
      results.push({
        label,
        delta: output.readFloat(),
        magnitude: output.add(4).readFloat(),
        returned,
      });
    };

    record('first');                       // establishes the origin, delta 0

    // Sleep so the next call sees a real, non-trivial elapsed time. The exact
    // duration does not matter; that it is not zero does.
    const sleeper = new NativeFunction(
        Module.getExportByName('libc.so', 'usleep'), 'int', ['uint']);
    sleeper(30000);                        // 30 ms

    record('second');                      // delta depends on the sleep
    record('third');                       // near-zero delta: calls are adjacent

    return {tid, results};
  },
};
