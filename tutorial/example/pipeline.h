// tutorial/example/pipeline.h
//
// Layer 2, 3, and 4: a parent function with child calls, and a time-dependent
// function.
//
// `pipeline_process` is the function you will reimplement. It calls three
// helpers. Two of them are private to the library, so you cannot call them:
// they are not exported, and in a real target you would never see their code.
// That is the whole point. You must reproduce the parent's behaviour while the
// helpers it depends on are unavailable to your build.
//
// The parent:
//   * decides, from its inputs, which helpers to call;
//   * calls them in a specific order;
//   * holds a lock across part of the work;
//   * writes its result into a caller-owned buffer.
//
// Every one of those is observable in a recording, and every one of them is a
// place a reimplementation can silently diverge.
//
// `pipeline_predict` adds the fourth dimension: it reads CLOCK_MONOTONIC and
// produces a value that depends on when it ran. A recording of this function is
// meaningless unless the time it read is recorded too.
#pragma once

#include <cstdint>

extern "C" {

// The public entry point. This is what you reimplement.
//
//   config   selects the code path
//   input    input samples
//   count    number of samples
//   out      caller-owned output buffer, at least 4 floats
//
// Returns the number of output floats written, or -1 on an invalid config.
int pipeline_process(int config, const float* input, int count, float* out);

// Predicts a delta from the time since the last call and the input rate.
//
//   input      input samples
//   count      number of samples
//   out        caller-owned output buffer, at least 2 floats
//
// Reads CLOCK_MONOTONIC. The first call has no previous timestamp, so it
// returns a zero delta and establishes the origin. Every later call returns the
// elapsed milliseconds since the previous call, scaled by the input magnitude.
//
// Because the result depends on wall-clock time between calls, a recording of
// this function is only reproducible if the clock readings are recorded and fed
// back during replay.
float pipeline_predict(const float* input, int count, float* out);

} // extern "C"
