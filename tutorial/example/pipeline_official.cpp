// tutorial/example/pipeline_official.cpp
//
// The "official" implementation of the pipeline. Built into libgeom.so.
//
// The helpers are hidden: not exported, no symbol in the dynamic table. A
// reimplementation cannot call them, so it has to reproduce the parent's
// behaviour on its own. The recording is what tells it how.
//
// This file also demonstrates the lock and the object identity that the replay
// must normalize. The lock is real and must be taken and released in the same
// order; the state object's address differs between processes and must be
// mapped to a stable identity.

#include "pipeline.h"

#include <cmath>
#include <cstring>
#include <pthread.h>

namespace {
} // namespace

// A process-global state object. Its address changes between runs; its identity
// does not. The recording refers to it by identity, not by address.
struct PipelineState {
    pthread_mutex_t mutex;
    float gain;
    int calls;
};

static PipelineState& state() {
    static PipelineState value = {PTHREAD_MUTEX_INITIALIZER, 2.0f, 0};
    return value;
}

// Hidden helpers.
//
// In a real target these would not be exported, and you would find their
// addresses with a disassembler (see the eval-ghidra project). They are
// exported here only so the tutorial can generate probe declarations by symbol
// name. The replay treats them as unavailable regardless: your reimplementation
// must not call them, because on the real target it could not.
extern "C" {

__attribute__((visibility("default"), noinline)) void pipeline_apply_gain(float* values, int count,
                                                                          float gain) {
    for (int i = 0; i < count; ++i) {
        values[i] *= gain;
    }
}

// Normalize in place. Returns the pre-normalization max.
__attribute__((visibility("default"), noinline)) float pipeline_normalize(float* values,
                                                                          int count) {
    float maximum = 0.0f;
    for (int i = 0; i < count; ++i) {
        const float magnitude = std::fabs(values[i]);
        if (magnitude > maximum) {
            maximum = magnitude;
        }
    }
    if (maximum > 0.0f) {
        for (int i = 0; i < count; ++i) {
            values[i] /= maximum;
        }
    }
    return maximum;
}

// Write a summary into the output buffer.
__attribute__((visibility("default"), noinline)) int pipeline_summarize(const float* values,
                                                                       int count, float scale,
                                                                       float* out) {
    float total = 0.0f;
    for (int i = 0; i < count; ++i) {
        total += values[i];
    }
    out[0] = total;
    out[1] = scale;
    out[2] = static_cast<float>(count);
    out[3] = total * scale;
    return 4;
}

} // extern "C"

extern "C" int pipeline_process(int config, const float* input, int count, float* out) {
    if (config < 0 || config > 2 || count <= 0 || count > 64) {
        return -1;
    }
    PipelineState& shared = state();

    // Local working copy; the parent does not modify the caller's input.
    float work[64];
    std::memcpy(work, input, sizeof(float) * static_cast<std::size_t>(count));

    pthread_mutex_lock(&shared.mutex);
    shared.calls += 1;
    pipeline_apply_gain(work, count, shared.gain);

    if (config == 0) {
        // Path A: gain only, then summarize. No normalization.
        const int written = pipeline_summarize(work, count, 1.0f, out);
        pthread_mutex_unlock(&shared.mutex);
        return written;
    }
    if (config == 1) {
        // Path B: gain, normalize, summarize. Scale is the pre-normalization peak.
        const float peak = pipeline_normalize(work, count);
        const int written = pipeline_summarize(work, count, peak, out);
        pthread_mutex_unlock(&shared.mutex);
        return written;
    }

    // Path C: normalize first, then gain, then summarize. The order matters:
    // applying gain before normalization changes the reported scale.
    const float peak = pipeline_normalize(work, count);
    pipeline_apply_gain(work, count, shared.gain);
    const int written = pipeline_summarize(work, count, peak, out);
    pthread_mutex_unlock(&shared.mutex);
    return written;
}

namespace {

// Reads CLOCK_MONOTONIC and returns it in nanoseconds.
//
// This is the point that makes time an input: the result depends on when the
// function ran, which a recording cannot reproduce unless the readings are
// captured too.
std::int64_t monotonic_now_ns() {
    timespec now{};
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return static_cast<std::int64_t>(now.tv_sec) * 1000000000LL
           + static_cast<std::int64_t>(now.tv_nsec);
}

// The previous call's timestamp, and the magnitude of the previous input. The
// delta between calls is what the prediction is built on.
std::int64_t g_previous_ns = 0;

} // namespace

extern "C" float pipeline_predict(const float* input, int count, float* out) {
    if (input == nullptr || out == nullptr || count <= 0) {
        return -1.0f;
    }
    const std::int64_t now_ns = monotonic_now_ns();
    float magnitude = 0.0f;
    for (int i = 0; i < count; ++i) {
        const float value = input[i] < 0.0f ? -input[i] : input[i];
        if (value > magnitude) {
            magnitude = value;
        }
    }

    if (g_previous_ns == 0) {
        // First call: no previous timestamp, so no delta. Establish the origin.
        g_previous_ns = now_ns;
        out[0] = 0.0f;
        out[1] = magnitude;
        return 0.0f;
    }

    const float elapsed_ms = static_cast<float>(now_ns - g_previous_ns) / 1000000.0f;
    g_previous_ns = now_ns;
    const float delta = elapsed_ms * magnitude;
    out[0] = delta;
    out[1] = magnitude;
    return delta;
}
