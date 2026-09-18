// tutorial/example/official.cpp
//
// The "official" implementation. In a real project this is a compiled shared
// library you cannot read; here we write it so the tutorial can be followed
// end to end and so the answer is known.
//
// Two details are deliberate:
//
//   * geom_scale computes a*b + 1.0f. Whether that fuses to an FMA changes the
//     last bit, so the recording is what defines the correct rounding.
//   * geom_direction uses sqrtf. On the host it may lower to a libm call; that
//     is exactly why replay must run in the same environment as the recording.

#include "geom.h"

#include <cmath>

extern "C" {

float geom_scale(float a, float b, float* out) {
    out[0] = a * b + 1.0f;
    out[1] = a - b;
    return out[0] + out[1];
}

void geom_direction(float x, float y, float z, float* out) {
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
