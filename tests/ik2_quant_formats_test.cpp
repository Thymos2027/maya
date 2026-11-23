// Real ggml CPU dequantizers are the independent oracle for the extra Mini quant formats.
#include "../src/ik2/ik2_kernels.cuh"
#include "ggml-quants.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <vector>

namespace k = ik2::k;
#define CU(call) do { auto e = (call); if (e != cudaSuccess) { std::fprintf(stderr, "%s: %s\n", #call, cudaGetErrorString(e)); std::exit(2); } } while (0)
int fails = 0;
void check(bool ok, const char* what, int type) {
    std::printf("%s type %d: %s\n", what, type, ok ? "ok" : "FAIL");
    fails += !ok;
}
template<class T> T* upload(const std::vector<T>& v) {
    T* p = nullptr;
    CU(cudaMalloc((void**) &p, v.size() * sizeof(T)));
    CU(cudaMemcpy(p, v.data(), v.size() * sizeof(T), cudaMemcpyHostToDevice));
    return p;
}
void reference(int type, const uint8_t* src, float* dst, size_t n) {
    switch (type) {
#define REF(T, name) case k::T: dequantize_row_##name((const block_##name*) src, dst, (int64_t) n); break
        REF(kIQ2_XS, iq2_xs);
        REF(kIQ2_S, iq2_s);
        REF(kIQ3_S, iq3_s);
        REF(kIQ4_NL, iq4_nl);
        REF(kQ3_K, q3_K);
        REF(kQ4_K, q4_K);
#undef REF
        case k::kBF16:
            for (size_t i = 0; i < n; ++i) {
                uint32_t bits = ((uint32_t) src[2*i] | ((uint32_t) src[2*i+1] << 8)) << 16;
                std::memcpy(dst+i, &bits, 4);
            }
            break;
        default: std::abort();
    }
}
std::vector<uint8_t> blocks(int type, size_t n) {
    std::mt19937 rng(17 + type);
    const size_t bv = k::block_values(type), bb = k::block_bytes(type);
    std::vector<uint8_t> raw(n / bv * bb);
    for (auto& x : raw) x = (uint8_t) rng();
    for (size_t b = 0; b < n / bv; ++b) {
        auto half = [&](size_t off) { uint16_t h = (uint16_t) (0x2800 | (rng() & 0x83ff)); std::memcpy(raw.data()+b*bb+off, &h, 2); };
        if (type == k::kBF16) {
            uint16_t bf = (uint16_t) ((rng() & 0x807f) | ((120 + rng()%7) << 7));
            std::memcpy(raw.data()+2*b, &bf, 2);
        } else if (type == k::kQ3_K) half(108);
        else { half(0); if (type == k::kQ4_K) half(2); }
    }
    return raw;
}
bool near(float a, double b, double budget) { return std::isfinite(a) && std::abs(a-b) <= budget; }
void test(int type, int bv, int bb) {
    check(k::block_values(type) == bv && k::block_bytes(type) == (size_t) bb, "geometry", type);
    const int N = 17, K = 2304;
    auto raw = blocks(type, N*K);
    std::vector<float> ref(N*K), host(N*K), gpu(N*K), x(K), y(N);
    reference(type, raw.data(), ref.data(), ref.size());
    k::decode_host(type, raw.data(), host.size(), host.data());
    auto* d = upload(raw);
    auto* out = upload(gpu);
    std::vector<uint16_t> half(N*K);
    auto* h = upload(half);
    k::decode_f32(type, d, gpu.size(), out, nullptr);
    k::decode_f16(type, d, half.size(), h, nullptr);
    CU(cudaMemcpy(gpu.data(), out, gpu.size()*4, cudaMemcpyDeviceToHost));
    CU(cudaMemcpy(half.data(), h, half.size()*2, cudaMemcpyDeviceToHost));
    bool ok = true, ok16 = true;
    for (size_t i = 0; i < ref.size(); ++i) {
        ok &= near(host[i], ref[i], 1e-6*(1+std::abs(ref[i]))) && near(gpu[i], ref[i], 1e-6*(1+std::abs(ref[i])));
        ok16 &= near(ggml_fp16_to_fp32(half[i]), ref[i], 1e-3*std::abs(ref[i])+6e-8);
    }
    check(ok, "CPU/GPU F32 versus ggml", type);
    check(ok16, "GPU F16 versus ggml", type);
    for (int i = 0; i < K; ++i) x[i] = (float) ((i*7)%31 - 15) / 32;
    auto* dx = upload(x);
    auto* dy = upload(y);
    k::mv_blocks(type, d, N, K, dx, dy, nullptr);
    CU(cudaMemcpy(y.data(), dy, N*4, cudaMemcpyDeviceToHost));
    bool mv = true;
    for (int r = 0; r < N; ++r) {
        double sum = 0, ab = 0;
        for (int i = 0; i < K; ++i) { double t = (double) ref[r*K+i]*x[i]; sum += t; ab += std::abs(t); }
        mv &= near(y[r], sum, 2e-5*(1+ab));
    }
    check(mv, "dense GEMV versus ggml", type);
    // Gate/up share a type and are adjacent in one expert; a warp pair uses row_dot_2.
    std::vector<const uint8_t*> pointers{d};
    auto* dp = upload(pointers);
    auto* dt = upload(std::vector<int>{0});
    k::experts_gate_up(dp, dt, 1, type, type, (size_t) 8*K/bv*bb, dx, K, 8, out, nullptr);
    CU(cudaMemcpy(y.data(), out, 16*4, cudaMemcpyDeviceToHost));
    bool expert = true;
    for (int r = 0; r < 16; ++r) {
        double sum = 0, ab = 0;
        for (int i = 0; i < K; ++i) { double t = (double) ref[r*K+i]*x[i]; sum += t; ab += std::abs(t); }
        expert &= near(y[r], sum, 2e-5*(1+ab));
    }
    check(expert, "expert gate/up versus ggml", type);
    std::vector<uint16_t> xhalf(K);
    for (int i = 0; i < K; ++i) xhalf[i] = ggml_fp32_to_fp16(x[i]);
    auto* dh = upload(xhalf);
    k::experts_down(dp, 1, type, 0, dh, N, K, dy, nullptr);
    CU(cudaMemcpy(y.data(), dy, N*4, cudaMemcpyDeviceToHost));
    bool down = true;
    for (int r = 0; r < N; ++r) {
        double sum = 0, ab = 0;
        for (int i = 0; i < K; ++i) { double t = (double) ref[r*K+i]*ggml_fp16_to_fp32(xhalf[i]); sum += t; ab += std::abs(t); }
        down &= near(y[r], sum, 2e-5*(1+ab));
    }
    check(down, "expert down versus ggml", type);
    CU(cudaFree(dh));
    CU(cudaFree(d)); CU(cudaFree(out)); CU(cudaFree(h)); CU(cudaFree(dx)); CU(cudaFree(dy)); CU(cudaFree(dp)); CU(cudaFree(dt));
}
int main() {
    auto* ctx = ggml_init({1024*1024, nullptr, true});
    test(k::kIQ2_XS,256,74); test(k::kIQ2_S,256,82); test(k::kIQ3_S,256,110);
    test(k::kIQ4_NL,32,18); test(k::kQ3_K,256,110); test(k::kQ4_K,256,144); test(k::kBF16,1,2);
    const std::vector<uint16_t> bits{0, 0x8000, 1, 0x007f, 0x0080, 0x7f80, 0xff80, 0x7fc1};
    auto* dbits = upload(bits);
    auto* decoded = upload(std::vector<float>(bits.size()));
    k::decode_f32(k::kBF16, (const uint8_t*) dbits, bits.size(), decoded, nullptr);
    std::vector<uint32_t> back(bits.size());
    CU(cudaMemcpy(back.data(), decoded, back.size()*4, cudaMemcpyDeviceToHost));
    bool exact = true;
    for (size_t i = 0; i < bits.size(); ++i) exact &= back[i] == ((uint32_t) bits[i] << 16);
    check(exact, "BF16 zero/subnormal/infinity/NaN bits", k::kBF16);
    CU(cudaFree(dbits)); CU(cudaFree(decoded));
    ggml_free(ctx);
    return fails ? 1 : 0;
}
