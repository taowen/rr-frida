// tutorial/example/geom.h
//
// A tiny library with two functions we want to reimplement exactly.
//
// The point of the tutorial is not that these functions are hard. It is that
// the pipeline records their *contract* (arguments, return value, memory
// effects) and then checks your own C++ against it byte for byte. A contract
// that small is enough to catch a wrong rounding mode, a wrong sign, or a
// swapped field.
#pragma once

#include <cstdint>

extern "C" {

// Fills `out` with two floats derived from `a` and `b`.
//   out[0] = a * b + 1.0f
//   out[1] = a - b
// Returns the sum of the two outputs.
float geom_scale(float a, float b, float* out);

// Writes a 3-float result into `out` from a direction vector.
// The implementation must be bit-identical, which matters when the caller
// feeds the result into further math.
void geom_direction(float x, float y, float z, float* out);

} // extern "C"
