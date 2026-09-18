// tutorial/example/driver.cpp
//
// A tiny Android process that runs the oracle library in a loop,
// so Frida has something to attach to without an APK or Gradle project.
//
// Build (AArch64 Android):
//   $NDK/clang++ --target=aarch64-linux-android29 -O2 -shared -fPIC \
//       official.cpp -o build/libgeom.so
//   $NDK/clang++ --target=aarch64-linux-android29 -O2 -static-libstdc++ \
//       -Wl,-rpath,/data/local/tmp -Lbuild -lgeom \
//       driver.cpp -o build/geom-driver
//
// Run on device:
//   adb push build/libgeom.so build/geom-driver /data/local/tmp/
//   adb shell /data/local/tmp/geom-driver
//
// The library is a separate module on purpose: Frida attaches by module name,
// which is how a real recording targets the library you want to replace.
//
// Inputs are compile-time constants so a recording is deterministic. A real
// driver would take its inputs from the outside.

#include "geom.h"

#include <cstdio>
#include <thread>
#include <chrono>

int main() {
    float scale_out[2] = {0.0f, 0.0f};
    float direction_out[3] = {0.0f, 0.0f, 0.0f};
    // Run until killed. A recording attaches while this loop is live, so the
    // process must outlive the attachment and the observation window.
    for (long iteration = 0;; ++iteration) {
        const float sum = geom_scale(2.0f, 3.0f, scale_out);
        // (1, 2, 3) is chosen because the float and double lengths round
        // differently, so a reimplementation that computes the length in
        // double produces a different last bit. See tutorial/README.md.
        geom_direction(1.0f, 2.0f, 3.0f, direction_out);
        if (iteration % 100000 == 0) {
            std::printf("%ld scale=%.9g dir=%.9g,%.9g,%.9g\n",
                        iteration, sum, direction_out[0], direction_out[1], direction_out[2]);
            std::fflush(stdout);
        }
        // Keep the call rate low enough that the recorder is not overwhelmed.
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return 0;
}
