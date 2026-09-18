// tutorial/replay_geom.cpp
//
// The replay program. It reads the validated input stream on stdin, calls the
// reimplementation, and writes the results on stdout in the same layout the
// adapter expects.
//
// It links `mine.cpp` only. It does not include or link `official.cpp`, and
// nothing here has seen the expected output: the comparator supplies the input,
// and the expected bytes are compared on the host afterwards.
//
// Input:
//   u32 scale_count
//   u32 direction_count
//   scale_count      * { float a; float b; }
//   direction_count  * { float x; float y; float z; }
//
// Output:
//   scale_count      * { float out0; float out1; float returned; }
//   direction_count  * { float out0; float out1; float out2; }

#include "geom.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace {

template <class T>
T read_value() {
    T value{};
    if (std::fread(&value, sizeof(value), 1, stdin) != 1) {
        throw std::runtime_error("truncated input");
    }
    return value;
}

template <class T>
void write_value(const T& value) {
    if (std::fwrite(&value, sizeof(value), 1, stdout) != 1) {
        throw std::runtime_error("output failed");
    }
}

} // namespace

int main() {
    try {
        const std::uint32_t scale_count = read_value<std::uint32_t>();
        const std::uint32_t direction_count = read_value<std::uint32_t>();

        for (std::uint32_t i = 0; i < scale_count; ++i) {
            const float a = read_value<float>();
            const float b = read_value<float>();
            float out[2] = {0.0f, 0.0f};
            const float returned = geom_scale(a, b, out);
            write_value(out[0]);
            write_value(out[1]);
            write_value(returned);
        }
        for (std::uint32_t i = 0; i < direction_count; ++i) {
            const float x = read_value<float>();
            const float y = read_value<float>();
            const float z = read_value<float>();
            float out[3] = {0.0f, 0.0f, 0.0f};
            geom_direction(x, y, z, out);
            write_value(out[0]);
            write_value(out[1]);
            write_value(out[2]);
        }
        if (std::fgetc(stdin) != EOF || std::ferror(stdin)) {
            throw std::runtime_error("trailing input");
        }
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "replay_geom: %s\n", error.what());
        return 2;
    }
}
