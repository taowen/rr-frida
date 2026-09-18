// tutorial/example/pipeline.h
//
// Layer 2 and 3: a parent function with child calls.
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

} // extern "C"
