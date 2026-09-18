// tutorial/replay_predict.cpp
//
// Replays `pipeline_predict` against a recording, feeding it the recorded time.
//
// The reimplementation never reads the clock. The replay resolves
// `predict_elapsed_ms()` from the recorded clock readings, so the function sees
// exactly the time the original saw.
//
// Input, per case:
//   i32 count
//   f32 input[count]
//   f32 elapsed_ms          the recorded time since the previous call
//
// Output, per case:
//   f32 delta
//   f32 magnitude
//   f32 returned
//
// The recording supplies one case per call, in order. The elapsed value is the
// difference between this call's recorded clock reading and the previous one;
// for the first call it is whatever the original observed, which the recorder
// captured as the difference from its own origin.

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <vector>

#include "pipeline.h"

namespace {

std::vector<float> g_elapsed;
std::size_t g_index = 0;

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

namespace tutorial_helpers {

// The recorded elapsed milliseconds for the current call.
float predict_elapsed_ms() {
    if (g_index >= g_elapsed.size()) {
        throw std::runtime_error("more predict calls than recorded elapsed values");
    }
    return g_elapsed[g_index++];
}

} // namespace tutorial_helpers

int main() {
    try {
        const std::uint32_t cases = read_value<std::uint32_t>();
        for (std::uint32_t c = 0; c < cases; ++c) {
            const std::int32_t count = read_value<std::int32_t>();
            if (count <= 0 || count > 64) {
                throw std::runtime_error("bad case count");
            }
            std::vector<float> input(static_cast<std::size_t>(count));
            for (std::int32_t i = 0; i < count; ++i) {
                input[static_cast<std::size_t>(i)] = read_value<float>();
            }
            g_elapsed.assign(1, read_value<float>());
            g_index = 0;

            float out[2] = {0.0f, 0.0f};
            const float returned = pipeline_predict(input.data(), count, out);
            write_value(out[0]);
            write_value(out[1]);
            write_value(returned);
        }
        if (std::fgetc(stdin) != EOF || std::ferror(stdin)) {
            throw std::runtime_error("trailing input");
        }
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "replay_predict: %s\n", error.what());
        return 2;
    }
}
