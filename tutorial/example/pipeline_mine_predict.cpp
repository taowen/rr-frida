// tutorial/example/pipeline_mine_predict.cpp
//
// Your reimplementation of `pipeline_predict`.
//
// The oracle reads CLOCK_MONOTONIC. Yours must not: a replay that
// reads the real clock produces a different answer every run, and the recorded
// result would never match.
//
// So the elapsed time is an **input**. The replay feeds it in. This file
// declares a dependency for the time value, exactly as a pointer argument is
// an input. That is the whole idea of a logical clock: time stops being an
// ambient fact and becomes recorded evidence.
//
//   elapsed_ms   the time since the previous call, in milliseconds, recorded
//                from the original run
//   input/count  the samples
//   out          caller-owned output buffer, at least 2 floats
//
// Returns the delta.

#include <cstdint>

namespace tutorial_helpers {
// Provided by the replay (or by your host in a real build). Returns the elapsed
// milliseconds since the previous call, from the recording.
float predict_elapsed_ms();
}

extern "C" float pipeline_predict(const float* input, int count, float* out) {
    if (input == nullptr || out == nullptr || count <= 0) {
        return -1.0f;
    }
    float magnitude = 0.0f;
    for (int i = 0; i < count; ++i) {
        const float value = input[i] < 0.0f ? -input[i] : input[i];
        if (value > magnitude) {
            magnitude = value;
        }
    }

    const float elapsed_ms = tutorial_helpers::predict_elapsed_ms();
    const float delta = elapsed_ms * magnitude;
    out[0] = delta;
    out[1] = magnitude;
    return delta;
}
