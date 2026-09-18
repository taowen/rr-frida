// tutorial/replay_pipeline.cpp
//
// Replays `pipeline_process` from pipeline_mine.cpp and checks its contract.
//
// How the child-call contract is checked
// --------------------------------------
// The official parent calls helpers that your build cannot call. You replaced
// them with your own. To compare behaviour, the replay does not try to
// intercept your helper calls. Instead it provides the helpers as shims that
// *report every call* (which helper, which order, which arguments) and return
// the recorded outcome. Your parent code calls the shims; the shims produce a
// child-call log; the adapter compares that log to the recording.
//
// This is the dependency-adapter boundary: the parent runs for real, the
// dependencies are supplied, and the observable interaction between them is
// what gets compared.
//
// Input, per case:
//   i32 config
//   i32 count
//   f32 input[count]
//   u32 child_count
//   child_count * { u32 kind; u32 argc; u64 args[4]; u64 result }
//
// Output, per case:
//   i32 written
//   f32 out[4]
//   u32 observed_children
//   observed_children * { u32 kind; u32 argc; u64 args[4] }

#include "pipeline.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace {

struct ChildCall {
    std::uint32_t kind;
    std::uint32_t argc;
    std::uint64_t args[4];
    std::uint64_t result;
};

std::vector<ChildCall> g_expected;
std::vector<ChildCall> g_observed;
std::size_t g_child_index = 0;

// Identity tokens for the buffers a child can receive. The recording uses the
// same tokens, derived from the same roles, so the two runs are comparable
// even though their addresses differ.
enum : std::uint64_t {
    IDENTITY_WORK = 1,   // the parent's private working copy
    IDENTITY_OUT = 2,    // the caller's output buffer
    IDENTITY_INPUT = 3,  // the caller's input buffer
};

const float* g_input_base = nullptr;
float* g_out_base = nullptr;
float* g_work_base = nullptr;

std::uint64_t identity_of(const void* p) {
    if (p == g_work_base) return IDENTITY_WORK;
    if (p == g_out_base) return IDENTITY_OUT;
    if (p == g_input_base) return IDENTITY_INPUT;
    return 0;   // unknown buffer: report zero, never a raw address
}

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

void fail(const char* what) {
    std::fprintf(stderr, "pipeline replay: %s\n", what);
    std::exit(2);
}
// Check the next child call against the recording, then return its result.
//
// Pointer arguments cannot be compared by value across processes: the official
// run and this run have different addresses. Only the *identity* of an argument
// matters -- is it the caller's input buffer, its output buffer, or the
// parent's private working copy? The adapter records each pointer as a small
// identity token and the replay does the same, so the two are comparable.
std::uint64_t contract_child(std::uint32_t kind, std::uint32_t argc,
                             std::initializer_list<std::uint64_t> args) {
    if (g_child_index >= g_expected.size()) {
        fail("parent made more child calls than the recording has");
    }
    const ChildCall& want = g_expected[g_child_index];
    if (want.kind != kind || want.argc != argc) {
        fail("child call identity or arity differs from the recording");
    }
    std::uint32_t i = 0;
    for (std::uint64_t value : args) {
        if (want.args[i] != value) {
            std::fprintf(stderr,
                         "pipeline replay: child %u arg %u: actual %llu != recorded %llu\n",
                         static_cast<unsigned>(g_child_index), i,
                         static_cast<unsigned long long>(value),
                         static_cast<unsigned long long>(want.args[i]));
            fail("child call argument differs from the recording");
        }
        ++i;
    }
    ChildCall observed{};
    observed.kind = kind;
    observed.argc = argc;
    std::memcpy(observed.args, want.args, sizeof(observed.args));
    g_observed.push_back(observed);
    ++g_child_index;
    return want.result;
}

} // namespace

// The helpers the reimplementation is allowed to call. Each reports to the
// contract checker using buffer identities instead of raw addresses.
namespace tutorial_helpers {

void register_work_buffer(float* values) {
    g_work_base = values;
}

void apply_gain(float* values, int count, float gain) {
    contract_child(310, 3, {identity_of(values),
                            static_cast<std::uint64_t>(count),
                            0});
    for (int i = 0; i < count; ++i) {
        values[i] *= gain;
    }
}

float normalize(float* values, int count) {
    contract_child(320, 2, {identity_of(values),
                            static_cast<std::uint64_t>(count)});
    // Recompute the same way the official helper did. The contract fixed that
    // the call happened with these arguments; the arithmetic is ours.
    float maximum = 0.0f;
    for (int i = 0; i < count; ++i) {
        const float magnitude = values[i] < 0.0f ? -values[i] : values[i];
        if (magnitude > maximum) {
            maximum = magnitude;
        }
    }
    if (maximum > 0.0f) {
        for (int i = 0; i < count; ++i) {
            values[i] /= maximum;
        }
    }
    return maximum;
}

int summarize(const float* values, int count, float scale, float* out) {
    contract_child(330, 4, {identity_of(values),
                            static_cast<std::uint64_t>(count),
                            0,
                            identity_of(out)});
    float total = 0.0f;
    for (int i = 0; i < count; ++i) {
        total += values[i];
    }
    out[0] = total;
    out[1] = scale;
    out[2] = static_cast<float>(count);
    out[3] = total * scale;
    return 4;
}

} // namespace tutorial_helpers

int main() {
    try {
        const std::uint32_t cases = read_value<std::uint32_t>();
        for (std::uint32_t c = 0; c < cases; ++c) {
            const std::int32_t config = read_value<std::int32_t>();
            const std::int32_t count = read_value<std::int32_t>();
            if (count < 0 || count > 64) {
                fail("bad case count");
            }
            std::vector<float> input(static_cast<std::size_t>(count));
            for (std::int32_t i = 0; i < count; ++i) {
                input[static_cast<std::size_t>(i)] = read_value<float>();
            }
            const std::uint32_t child_count = read_value<std::uint32_t>();
            g_expected.assign(child_count, ChildCall{});
            for (std::uint32_t i = 0; i < child_count; ++i) {
                g_expected[i].kind = read_value<std::uint32_t>();
                g_expected[i].argc = read_value<std::uint32_t>();
                for (auto& value : g_expected[i].args) {
                    value = read_value<std::uint64_t>();
                }
                g_expected[i].result = read_value<std::uint64_t>();
            }
            g_observed.clear();
            g_child_index = 0;

            float out[4] = {0.0f, 0.0f, 0.0f, 0.0f};
            g_input_base = input.data();
            g_out_base = out;
            g_work_base = nullptr;
            const int written = pipeline_process(config, input.data(), count, out);

            if (g_child_index != g_expected.size()) {
                fail("parent made fewer child calls than the recording has");
            }
            write_value<std::int32_t>(written);
            for (float value : out) {
                write_value(value);
            }
            write_value<std::uint32_t>(static_cast<std::uint32_t>(g_observed.size()));
            for (const ChildCall& call : g_observed) {
                write_value(call.kind);
                write_value(call.argc);
                for (std::uint64_t value : call.args) {
                    write_value(value);
                }
            }
        }
        if (std::fgetc(stdin) != EOF || std::ferror(stdin)) {
            throw std::runtime_error("trailing input");
        }
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "replay_pipeline: %s\n", error.what());
        return 2;
    }
}
