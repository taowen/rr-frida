// tutorial/example/mine.cpp
//
// Your reimplementation. It is *supposed* to be equivalent to official.cpp.
//
// This version is correct. `git log` shows the earlier revision, which computed
// the length in double precision; the compare step rejected it with
//   direction[0] byte 8: actual 0xb3 != official 0xb2
// a one-ULP difference in the third component. Change the length back to
// double and rerun the compare to see the guard fire again.

#include "geom.h"

#include <cmath>

extern "C" {

float geom_scale(float a, float b, float* out) {
    out[0] = a * b + 1.0f;
    out[1] = a - b;
    return out[0] + out[1];
}

void geom_direction(float x, float y, float z, float* out) {
    // Compute the length in float, exactly as the official function does.
    const float length = std::sqrt(x * x + y * y + z * z);
    if (length > 0.0f) {
        out[0] = x / length;
        out[1] = y / length;
        out[2] = z / length;
    } else {
        out[0] = 0.0f;
        out[1] = 0.0f;
        out[2] = 0.0f;
    }
}

} // extern "C"
