// tutorial/example/pipeline_mine.cpp
//
// Your reimplementation of `pipeline_process`.
//
// It does not define the helpers. It calls them through the names declared
// below, which the replay provides as shims. That mirrors the real situation:
// the helpers belong to the library you are replacing, so your build supplies
// its own — and the replay validates the interaction.
//
// The code is correct. Read tutorial/README.md for the bug an earlier revision
// had and why a value-only test would not have caught it.

#include "pipeline.h"

#include <cstring>

// The dependency boundary. These are defined by the replay (or by your own
// library in a real build); the parent only sees these declarations.
namespace tutorial_helpers {
void apply_gain(float* values, int count, float gain);
float normalize(float* values, int count);
int summarize(const float* values, int count, float scale, float* out);
// Registers a buffer so its identity is known when a child is called. The
// replay uses this to compare pointer arguments by role, not by address.
void register_work_buffer(float* values);
} // namespace tutorial_helpers

extern "C" int pipeline_process(int config, const float* input, int count, float* out) {
    if (config < 0 || config > 2 || count <= 0 || count > 64) {
        return -1;
    }

    float work[64];
    tutorial_helpers::register_work_buffer(work);
    std::memcpy(work, input, sizeof(float) * static_cast<std::size_t>(count));

    tutorial_helpers::apply_gain(work, count, 2.0f);

    if (config == 0) {
        return tutorial_helpers::summarize(work, count, 1.0f, out);
    }
    if (config == 1) {
        const float peak = tutorial_helpers::normalize(work, count);
        return tutorial_helpers::summarize(work, count, peak, out);
    }

    // Path C: normalize first, then gain. The scale is the pre-gain peak.
    const float peak = tutorial_helpers::normalize(work, count);
    tutorial_helpers::apply_gain(work, count, 2.0f);
    return tutorial_helpers::summarize(work, count, peak, out);
}
