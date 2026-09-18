// tutorial/example/pipeline_driver.cpp
//
// Runs pipeline_process across all three code paths so a recording covers the
// branch structure, not just one input. Each iteration cycles config 0, 1, 2
// with the same samples, so a recording of a few hundred calls contains every
// path many times.

#include "pipeline.h"

#include <chrono>
#include <cstdio>
#include <thread>

int main() {
    const float input[8] = {1.0f, -2.5f, 0.5f, 4.0f, -0.25f, 3.0f, 1.5f, -1.0f};
    float out[8] = {0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f};
    for (long iteration = 0;; ++iteration) {
        const int config = static_cast<int>(iteration % 3);
        const int written = pipeline_process(config, input, 8, out);
        if (iteration % 100000 == 0) {
            std::printf("%ld config=%d written=%d out=%.9g,%.9g,%.9g,%.9g\n",
                        iteration, config, written, out[0], out[1], out[2], out[3]);
            std::fflush(stdout);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return 0;
}
