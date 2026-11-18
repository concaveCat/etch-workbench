// High-performance C++17 + OpenMP volumetric kernels for engines.voxel.
//
// Enables full-window 1 nm x 1 nm x 1 nm simulation (e.g. 624 x 740 x 2781 =
// 1.284B voxels on Memory1) in < 2 GiB peak RAM and a few seconds of wall time
// by replacing 15+ GiB 3D float32 temporaries with Z-slabbed linear-time O(N)
// Felzenszwalb-Huttenlocher separable 3D Euclidean distance transforms,
// active-Z band pruning, and zero-allocation directional cavity flood-fills.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

namespace {

static void edt_1d_periodic(
    const float* in_f,
    float* out_f,
    int n,
    double h,
    double r_tol,
    double cap_inf,
    std::vector<double>& ft,
    std::vector<int>& v,
    std::vector<double>& z
) {
    if (n <= 1 || r_tol <= 0.0) {
        for (int i = 0; i < n; ++i) {
            out_f[i] = std::min(in_f[i], static_cast<float>(cap_inf));
        }
        return;
    }

    const int r_pad = std::min(n, static_cast<int>(std::floor(r_tol / h)) + 1);
    const int m = n + 2 * r_pad;
    const double h2 = h * h;
    const double inv_h2 = 1.0 / h2;

    if (static_cast<int>(ft.size()) < m) {
        ft.resize(m);
        v.resize(m + 1);
        z.resize(m + 2);
    }

    for (int i = 0; i < r_pad; ++i) {
        const double val = std::min(static_cast<double>(in_f[n - r_pad + i]), cap_inf);
        ft[i] = val * inv_h2;
    }
    for (int i = 0; i < n; ++i) {
        const double val = std::min(static_cast<double>(in_f[i]), cap_inf);
        ft[r_pad + i] = val * inv_h2;
    }
    for (int i = 0; i < r_pad; ++i) {
        const double val = std::min(static_cast<double>(in_f[i]), cap_inf);
        ft[r_pad + n + i] = val * inv_h2;
    }

    int k = 0;
    v[0] = 0;
    z[0] = -1e30;
    z[1] = +1e30;

    for (int q = 1; q < m; ++q) {
        const double fq_plus_q2 = ft[q] + static_cast<double>(q) * static_cast<double>(q);
        double s = 0.0;
        while (true) {
            const int vk = v[k];
            const double fvk_plus_vk2 = ft[vk] + static_cast<double>(vk) * static_cast<double>(vk);
            s = (fq_plus_q2 - fvk_plus_vk2) / (2.0 * static_cast<double>(q - vk));
            if (k > 0 && s <= z[k]) {
                --k;
            } else {
                break;
            }
        }
        ++k;
        v[k] = q;
        z[k] = s;
        z[k + 1] = +1e30;
    }

    int k_ptr = 0;
    for (int q_out = 0; q_out < n; ++q_out) {
        const int q = q_out + r_pad;
        while (z[k_ptr + 1] < static_cast<double>(q)) {
            ++k_ptr;
        }
        const double diff = static_cast<double>(q - v[k_ptr]);
        out_f[q_out] = static_cast<float>((diff * diff + ft[v[k_ptr]]) * h2);
    }
}

struct EdtThreadWorkspace {
    std::vector<float> line_in;
    std::vector<float> line_out;
    std::vector<double> ft;
    std::vector<int> v;
    std::vector<double> z;

    void ensure(int nx, int ny) {
        const size_t cap = static_cast<size_t>(std::max(nx, ny));
        if (line_in.size() < cap) {
            line_in.resize(cap);
            line_out.resize(cap);
        }
    }
};

static void edt_2d_slice_inplace(
    float* slice_f,
    int nx,
    int ny,
    double dx,
    double dy,
    double r_tol,
    double cap_inf,
    EdtThreadWorkspace& ws
) {
    ws.ensure(nx, ny);

    if (ny > 1) {
        for (int ix = 0; ix < nx; ++ix) {
            float* row = slice_f + static_cast<size_t>(ix) * ny;
            edt_1d_periodic(row, ws.line_out.data(), ny, dy, r_tol, cap_inf, ws.ft, ws.v, ws.z);
            std::memcpy(row, ws.line_out.data(), static_cast<size_t>(ny) * sizeof(float));
        }
    }

    if (nx > 1) {
        for (int iy = 0; iy < ny; ++iy) {
            for (int ix = 0; ix < nx; ++ix) {
                ws.line_in[ix] = slice_f[static_cast<size_t>(ix) * ny + iy];
            }
            edt_1d_periodic(ws.line_in.data(), ws.line_out.data(), nx, dx, r_tol, cap_inf, ws.ft, ws.v, ws.z);
            for (int ix = 0; ix < nx; ++ix) {
                slice_f[static_cast<size_t>(ix) * ny + iy] = ws.line_out[ix];
            }
        }
    }
}

static inline void propagate_column_z(
    const uint8_t* g, uint8_t* m, int b0, int b1, int k_limit
) {
    for (int k = std::max(1, b0); k <= b1; ++k) {
        if (g[k] == 0 && m[k] == 0 && m[k - 1] != 0) {
            m[k] = 1;
        }
    }
    for (int k = std::min(k_limit - 2, b1); k >= b0; --k) {
        if (g[k] == 0 && m[k] == 0 && m[k + 1] != 0) {
            m[k] = 1;
        }
    }
}

// Z-slabbed exact 3D Euclidean dilation over output slices k in [k_eval_min, kz).
// Sources in [max(0, k_eval_min - r_z - 2), kz) are scanned; no source below that
// can ever be within r_tol of any slice k >= k_eval_min.
template <typename SrcPred, typename SliceHasTargetFn, typename HitFn>
static void run_slabbed_3d_dilation(
    int nx,
    int ny,
    int kz,
    int k_eval_min,
    double radius_um,
    double dx,
    double dy,
    double dz,
    SrcPred src_pred,
    SliceHasTargetFn slice_has_target,
    HitFn hit_fn
) {
    if (kz <= 0 || k_eval_min >= kz || radius_um <= 0.0) {
        return;
    }
    k_eval_min = std::max(0, k_eval_min);

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    const double r_tol = radius_um + 0.45 * dz;
    const double r_tol_sq = r_tol * r_tol;
    const float r_tol_sq_f = static_cast<float>(r_tol_sq);
    const double cap_inf = 4.0 * r_tol_sq + 1.0;
    const float cap_inf_f = static_cast<float>(cap_inf);
    const double dz2 = dz * dz;

    const int rz_margin = static_cast<int>(std::ceil(r_tol / dz)) + 2;
    const int k_src_min = std::max(0, k_eval_min - rz_margin);

    constexpr int SLAB_Z = 64;
    constexpr int INF_K = 100000000;

    std::unique_ptr<int[]> last_one(new int[n_cols]);
    std::unique_ptr<int[]> after_ptr(new int[n_cols]);
    std::unique_ptr<float[]> g_slab(new float[static_cast<size_t>(SLAB_Z) * n_cols]);

    int any_src = 0;
    #pragma omp parallel for schedule(static) reduction(|:any_src)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        const size_t c = static_cast<size_t>(col);
        // Find highest source in [k_src_min, k_eval_min)
        int prev = -INF_K;
        for (int k = k_eval_min - 1; k >= k_src_min; --k) {
            if (src_pred(c, k)) {
                prev = k;
                break;
            }
        }
        last_one[c] = prev;

        // Find first source in [k_eval_min, kz)
        int p = k_eval_min;
        while (p < kz && !src_pred(c, p)) {
            ++p;
        }
        after_ptr[c] = p;
        if (prev != -INF_K || p < kz) {
            any_src = 1;
        }
    }
    if (!any_src) {
        return;
    }

    for (int k0 = k_eval_min; k0 < kz; k0 += SLAB_Z) {
        const int k1 = std::min(kz, k0 + SLAB_Z);
        const int s_len = k1 - k0;

        #pragma omp parallel for schedule(static)
        for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
            const size_t c = static_cast<size_t>(col);
            int prev = last_one[c];
            int local_dk[SLAB_Z];

            for (int k = k0; k < k1; ++k) {
                if (src_pred(c, k)) {
                    prev = k;
                }
                local_dk[k - k0] = k - prev;
            }
            last_one[c] = prev;

            int aptr = after_ptr[c];
            if (aptr < k1) {
                aptr = k1;
                while (aptr < kz && !src_pred(c, aptr)) {
                    ++aptr;
                }
                after_ptr[c] = aptr;
            }

            int nxt = (aptr < kz) ? aptr : INF_K;
            for (int k = k1 - 1; k >= k0; --k) {
                if (src_pred(c, k)) {
                    nxt = k;
                }
                const int up_dk = nxt - k;
                const int dk = std::min(local_dk[k - k0], up_dk);
                const double d2 = static_cast<double>(dk) * static_cast<double>(dk) * dz2;
                g_slab[static_cast<size_t>(k - k0) * n_cols + c] =
                    (d2 >= cap_inf) ? cap_inf_f : static_cast<float>(d2);
            }
        }

        #pragma omp parallel
        {
            EdtThreadWorkspace ws;
            #pragma omp for schedule(dynamic, 1)
            for (int s = 0; s < s_len; ++s) {
                const int k = k0 + s;
                if (!slice_has_target(k)) {
                    continue;
                }
                float* slice = g_slab.get() + static_cast<size_t>(s) * n_cols;

                float min_g = slice[0];
                float max_g = slice[0];
                for (size_t c = 1; c < n_cols; ++c) {
                    const float val = slice[c];
                    if (val < min_g) min_g = val;
                    if (val > max_g) max_g = val;
                }

                if (min_g > r_tol_sq_f) {
                    continue;
                }
                if (max_g <= r_tol_sq_f) {
                    for (size_t c = 0; c < n_cols; ++c) {
                        hit_fn(c, k);
                    }
                    continue;
                }

                edt_2d_slice_inplace(slice, nx, ny, dx, dy, r_tol, cap_inf, ws);
                for (size_t c = 0; c < n_cols; ++c) {
                    if (slice[c] <= r_tol_sq_f) {
                        hit_fn(c, k);
                    }
                }
            }
        }
    }
}

// Compute exposed_void_mask and return the minimum k in [0, k_limit) containing exposed void
// (or k_limit if none exists).
static int compute_exposed_void_mask_internal(
    const uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int k_limit,
    uint8_t* out_mask
) {
    if (k_limit <= 0) return 0;
    const size_t n_cols_sz = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(n_cols_sz);
    std::unique_ptr<int32_t[]> col_bmin(new int32_t[n_cols_sz]);
    std::unique_ptr<int32_t[]> col_bmax(new int32_t[n_cols_sz]);

    int any_buried_void = 0;
    int min_exp_k = k_limit;

    #pragma omp parallel for schedule(static) reduction(|:any_buried_void) reduction(min:min_exp_k)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        const uint8_t* g = grid + static_cast<size_t>(col) * nz;
        uint8_t* m = out_mask + static_cast<size_t>(col) * k_limit;
        int k = k_limit - 1;
        while (k >= 0 && g[k] == 0) {
            m[k] = 1;
            --k;
        }
        if (k + 1 < min_exp_k) {
            min_exp_k = k + 1;
        }
        int b0 = k_limit;
        int b1 = -1;
        for (; k >= 0; --k) {
            m[k] = 0;
            if (g[k] == 0) {
                if (b1 < 0) b1 = k;
                b0 = k;
            }
        }
        col_bmin[col] = b0;
        col_bmax[col] = b1;
        if (b1 >= b0) {
            any_buried_void = 1;
        }
    }

    if (!any_buried_void) {
        return min_exp_k;
    }

    const int max_sweeps = std::max(nx, ny) + 8;
    for (int iter = 0; iter < max_sweeps; ++iter) {
        int iter_changed = 0;

        if (nx > 1) {
            #pragma omp parallel for schedule(static) reduction(|:iter_changed)
            for (int iy = 0; iy < ny; ++iy) {
                bool row_changed = false;
                for (int step = 0; step <= nx; ++step) {
                    const int ix = step % nx;
                    const size_t col = static_cast<size_t>(ix) * ny + iy;
                    const int b0 = col_bmin[col];
                    const int b1 = col_bmax[col];
                    if (b1 < b0) continue;

                    const int ix_prev = (ix == 0) ? (nx - 1) : (ix - 1);
                    const size_t col_prev = static_cast<size_t>(ix_prev) * ny + iy;
                    const uint8_t* g = grid + col * nz;
                    uint8_t* m = out_mask + col * k_limit;
                    const uint8_t* m_prev = out_mask + col_prev * k_limit;

                    bool col_hit = false;
                    for (int k = b0; k <= b1; ++k) {
                        if (g[k] == 0 && m[k] == 0 && m_prev[k] != 0) {
                            m[k] = 1;
                            col_hit = true;
                        }
                    }
                    if (col_hit) {
                        propagate_column_z(g, m, b0, b1, k_limit);
                        row_changed = true;
                    }
                }
                for (int step = 0; step <= nx; ++step) {
                    const int ix = (nx - 1 - (step % nx));
                    const size_t col = static_cast<size_t>(ix) * ny + iy;
                    const int b0 = col_bmin[col];
                    const int b1 = col_bmax[col];
                    if (b1 < b0) continue;

                    const int ix_next = (ix == nx - 1) ? 0 : (ix + 1);
                    const size_t col_next = static_cast<size_t>(ix_next) * ny + iy;
                    const uint8_t* g = grid + col * nz;
                    uint8_t* m = out_mask + col * k_limit;
                    const uint8_t* m_next = out_mask + col_next * k_limit;

                    bool col_hit = false;
                    for (int k = b0; k <= b1; ++k) {
                        if (g[k] == 0 && m[k] == 0 && m_next[k] != 0) {
                            m[k] = 1;
                            col_hit = true;
                        }
                    }
                    if (col_hit) {
                        propagate_column_z(g, m, b0, b1, k_limit);
                        row_changed = true;
                    }
                }
                if (row_changed) iter_changed = 1;
            }
        }

        if (ny > 1) {
            #pragma omp parallel for schedule(static) reduction(|:iter_changed)
            for (int ix = 0; ix < nx; ++ix) {
                bool col_group_changed = false;
                for (int step = 0; step <= ny; ++step) {
                    const int iy = step % ny;
                    const size_t col = static_cast<size_t>(ix) * ny + iy;
                    const int b0 = col_bmin[col];
                    const int b1 = col_bmax[col];
                    if (b1 < b0) continue;

                    const int iy_prev = (iy == 0) ? (ny - 1) : (iy - 1);
                    const size_t col_prev = static_cast<size_t>(ix) * ny + iy_prev;
                    const uint8_t* g = grid + col * nz;
                    uint8_t* m = out_mask + col * k_limit;
                    const uint8_t* m_prev = out_mask + col_prev * k_limit;

                    bool col_hit = false;
                    for (int k = b0; k <= b1; ++k) {
                        if (g[k] == 0 && m[k] == 0 && m_prev[k] != 0) {
                            m[k] = 1;
                            col_hit = true;
                        }
                    }
                    if (col_hit) {
                        propagate_column_z(g, m, b0, b1, k_limit);
                        col_group_changed = true;
                    }
                }
                for (int step = 0; step <= ny; ++step) {
                    const int iy = (ny - 1 - (step % ny));
                    const size_t col = static_cast<size_t>(ix) * ny + iy;
                    const int b0 = col_bmin[col];
                    const int b1 = col_bmax[col];
                    if (b1 < b0) continue;

                    const int iy_next = (iy == ny - 1) ? 0 : (iy + 1);
                    const size_t col_next = static_cast<size_t>(ix) * ny + iy_next;
                    const uint8_t* g = grid + col * nz;
                    uint8_t* m = out_mask + col * k_limit;
                    const uint8_t* m_next = out_mask + col_next * k_limit;

                    bool col_hit = false;
                    for (int k = b0; k <= b1; ++k) {
                        if (g[k] == 0 && m[k] == 0 && m_next[k] != 0) {
                            m[k] = 1;
                            col_hit = true;
                        }
                    }
                    if (col_hit) {
                        propagate_column_z(g, m, b0, b1, k_limit);
                        col_group_changed = true;
                    }
                }
                if (col_group_changed) iter_changed = 1;
            }
        }

        if (!iter_changed) {
            break;
        }
    }

    // Lower min_exp_k if any newly connected buried cavity sits deeper than open trenches:
    #pragma omp parallel for schedule(static) reduction(min:min_exp_k)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        const int b0 = col_bmin[col];
        const int b1 = col_bmax[col];
        if (b1 < b0 || b0 >= min_exp_k) continue;
        const uint8_t* m = out_mask + static_cast<size_t>(col) * k_limit;
        for (int k = b0; k <= std::min(b1, min_exp_k - 1); ++k) {
            if (m[k] != 0) {
                min_exp_k = k;
                break;
            }
        }
    }
    return min_exp_k;
}

}  // namespace

extern "C" {

void voxel_fill_z_band(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ka,
    int kb,
    uint8_t val
) {
    if (ka >= kb || ka >= nz || kb <= 0) return;
    ka = std::max(0, ka);
    kb = std::min(nz, kb);
    const size_t len = static_cast<size_t>(kb - ka);
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);

    #pragma omp parallel for schedule(static)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        std::memset(grid + static_cast<size_t>(col) * nz + ka, val, len);
    }
}

int voxel_active_k_top(const uint8_t* grid, int nx, int ny, int nz) {
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    int best_k = 0;

    #pragma omp parallel for schedule(static) reduction(max:best_k)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        const uint8_t* c = grid + static_cast<size_t>(col) * nz;
        int k = nz - 1;
        while (k - 7 >= best_k) {
            uint64_t chunk = 0;
            std::memcpy(&chunk, c + (k - 7), sizeof(uint64_t));
            if (chunk != 0) {
                break;
            }
            k -= 8;
        }
        for (; k >= best_k; --k) {
            if (c[k] != 0) {
                best_k = k + 1;
                break;
            }
        }
    }
    return best_k;
}

void voxel_column_tops_k(
    const uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    int32_t* out_tops
) {
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    const int k_lim = std::min(nz, std::max(0, ktop));

    #pragma omp parallel for schedule(static)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        const uint8_t* c = grid + static_cast<size_t>(col) * nz;
        int top = 0;
        for (int k = k_lim - 1; k >= 0; --k) {
            if (c[k] != 0) {
                top = k + 1;
                break;
            }
        }
        out_tops[col] = top;
    }
}

int voxel_material_max_k(
    const uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    uint8_t mid
) {
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    const int k_lim = std::min(nz, std::max(0, ktop));
    int best_k = 0;

    #pragma omp parallel for schedule(static) reduction(max:best_k)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        const uint8_t* c = grid + static_cast<size_t>(col) * nz;
        for (int k = k_lim - 1; k >= best_k; --k) {
            if (c[k] == mid) {
                best_k = k + 1;
                break;
            }
        }
    }
    return best_k;
}

void voxel_exposed_void_mask(
    const uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int k_limit,
    uint8_t* out_mask
) {
    compute_exposed_void_mask_internal(grid, nx, ny, nz, k_limit, out_mask);
}

void voxel_dilate_3d(
    const uint8_t* mask,
    int nx,
    int ny,
    int kz,
    double radius_um,
    double dx,
    double dy,
    double dz,
    uint8_t* out_mask
) {
    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    const size_t total = n_cols * static_cast<size_t>(kz);
    if (radius_um <= 0.0) {
        std::memcpy(out_mask, mask, total);
        return;
    }
    #pragma omp parallel for schedule(static)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        std::memset(out_mask + static_cast<size_t>(col) * kz, 0, static_cast<size_t>(kz));
    }
    run_slabbed_3d_dilation(
        nx, ny, kz, 0, radius_um, dx, dy, dz,
        [&](size_t col, int k) -> bool {
            return mask[col * static_cast<size_t>(kz) + k] != 0;
        },
        [&](int /*k*/) -> bool { return true; },
        [&](size_t col, int k) {
            out_mask[col * static_cast<size_t>(kz) + k] = 1;
        }
    );
}

int64_t voxel_depo_fill_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int k_target,
    uint8_t mid
) {
    k_target = std::min(nz, std::max(0, k_target));
    if (k_target <= 0) return 0;

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    std::unique_ptr<uint8_t[]> exposed(new uint8_t[n_cols * static_cast<size_t>(k_target)]);
    const int min_exp_k = compute_exposed_void_mask_internal(
        grid, nx, ny, nz, k_target, exposed.get()
    );
    if (min_exp_k >= k_target) return 0;

    int64_t filled = 0;
    #pragma omp parallel for schedule(static) reduction(+:filled)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        const uint8_t* e = exposed.get() + static_cast<size_t>(col) * k_target;
        int64_t local = 0;
        for (int k = min_exp_k; k < k_target; ++k) {
            if (e[k] != 0) {
                g[k] = mid;
                ++local;
            }
        }
        filled += local;
    }
    return filled;
}

int64_t voxel_depo_isotropic_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int k_lim,
    uint8_t mid,
    double thickness_um,
    double dx,
    double dy,
    double dz
) {
    k_lim = std::min(nz, std::max(0, k_lim));
    if (k_lim <= 0 || thickness_um <= 0.0) return 0;

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    std::unique_ptr<uint8_t[]> exposed(new uint8_t[n_cols * static_cast<size_t>(k_lim)]);
    const int min_exp_k = compute_exposed_void_mask_internal(
        grid, nx, ny, nz, k_lim, exposed.get()
    );
    if (min_exp_k >= k_lim) return 0;

    // Precompute per-Z-slice indicator: does slice k have ANY exposed_gas cell?
    std::vector<uint8_t> slice_has_gas(k_lim, 0);
    #pragma omp parallel for schedule(static)
    for (int k = min_exp_k; k < k_lim; ++k) {
        for (size_t c = 0; c < n_cols; ++c) {
            if ((exposed[c * static_cast<size_t>(k_lim) + k] & 1u) != 0u) {
                slice_has_gas[k] = 1;
                break;
            }
        }
    }

    run_slabbed_3d_dilation(
        nx, ny, k_lim, min_exp_k, thickness_um, dx, dy, dz,
        [&](size_t col, int k) -> bool {
            return grid[col * static_cast<size_t>(nz) + k] != 0;
        },
        [&](int k) -> bool {
            return slice_has_gas[k] != 0;
        },
        [&](size_t col, int k) {
            uint8_t& flag = exposed[col * static_cast<size_t>(k_lim) + k];
            if ((flag & 1u) != 0u) {
                flag |= 2u;
            }
        }
    );

    int64_t deposited = 0;
    #pragma omp parallel for schedule(static) reduction(+:deposited)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        const uint8_t* e = exposed.get() + static_cast<size_t>(col) * k_lim;
        int64_t local = 0;
        for (int k = min_exp_k; k < k_lim; ++k) {
            if ((e[k] & 2u) != 0u) {
                g[k] = mid;
                ++local;
            }
        }
        deposited += local;
    }
    return deposited;
}

int64_t voxel_depo_directional_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    int dk,
    uint8_t mid
) {
    if (dk <= 0) return 0;
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    const int k_lim = std::min(nz, std::max(0, ktop));
    int64_t deposited = 0;

    #pragma omp parallel for schedule(static) reduction(+:deposited)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        int k0 = 0;
        for (int k = k_lim - 1; k >= 0; --k) {
            if (g[k] != 0) {
                k0 = k + 1;
                break;
            }
        }
        const int k1 = std::min(nz, k0 + dk);
        if (k1 > k0) {
            std::memset(g + k0, mid, static_cast<size_t>(k1 - k0));
            deposited += (k1 - k0);
        }
    }
    return deposited;
}

int64_t voxel_top_down_etch_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    const double* rate_lut_256,
    double duration,
    double dz
) {
    const int k_lim = std::min(nz, std::max(0, ktop));
    if (k_lim <= 0 || duration <= 0.0) return 0;

    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    int64_t removed = 0;

    #pragma omp parallel for schedule(static) reduction(+:removed)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        uint8_t* c = grid + static_cast<size_t>(col) * nz;
        double rem_t = duration;
        int k = k_lim - 1;
        int64_t local_removed = 0;

        while (k >= 0 && rem_t > 1e-12) {
            while (k >= 0 && c[k] == 0) {
                --k;
            }
            if (k < 0) break;
            const uint8_t mid = c[k];
            const double rate = rate_lut_256[mid];
            if (rate <= 0.0) {
                break;
            }
            int k_run_start = k;
            while (k_run_start > 0 && c[k_run_start - 1] == mid) {
                --k_run_start;
            }
            const int run_cells = k - k_run_start + 1;
            const double max_cells = rem_t * rate / dz;
            const int etch_cells = std::min(
                run_cells,
                static_cast<int>(std::floor(max_cells + 0.5 + 1e-9))
            );
            if (etch_cells <= 0) {
                break;
            }
            std::memset(c + (k - etch_cells + 1), 0, static_cast<size_t>(etch_cells));
            local_removed += etch_cells;
            rem_t -= (static_cast<double>(etch_cells) * dz) / rate;
            k -= etch_cells;
        }
        removed += local_removed;
    }
    return removed;
}

int64_t voxel_etch_isotropic_substep_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    int n_groups,
    const double* radii_um,
    const uint8_t* group_luts_256,
    double dx,
    double dy,
    double dz
) {
    const int k_lim = std::min(nz, std::max(0, ktop));
    if (k_lim <= 0 || n_groups <= 0) return 0;

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    std::unique_ptr<uint8_t[]> exposed(new uint8_t[n_cols * static_cast<size_t>(k_lim)]);
    const int min_exp_k = compute_exposed_void_mask_internal(
        grid, nx, ny, nz, k_lim, exposed.get()
    );
    if (min_exp_k >= k_lim) return 0;

    int min_eval_overall = k_lim;

    for (int g_idx = 0; g_idx < n_groups; ++g_idx) {
        const double radius = radii_um[g_idx];
        if (radius <= 0.0) continue;
        const uint8_t* lut = group_luts_256 + static_cast<size_t>(g_idx) * 256;
        const int rz_margin = static_cast<int>(std::ceil((radius + 0.45 * dz) / dz)) + 2;
        const int k_eval_min = std::max(0, min_exp_k - rz_margin);
        min_eval_overall = std::min(min_eval_overall, k_eval_min);

        std::vector<uint8_t> slice_has_mat(k_lim, 0);
        #pragma omp parallel for schedule(static)
        for (int k = k_eval_min; k < k_lim; ++k) {
            for (size_t c = 0; c < n_cols; ++c) {
                if (lut[grid[c * static_cast<size_t>(nz) + k]] != 0) {
                    slice_has_mat[k] = 1;
                    break;
                }
            }
        }

        run_slabbed_3d_dilation(
            nx, ny, k_lim, k_eval_min, radius, dx, dy, dz,
            [&](size_t col, int k) -> bool {
                return (exposed[col * static_cast<size_t>(k_lim) + k] & 1u) != 0u;
            },
            [&](int k) -> bool {
                return slice_has_mat[k] != 0;
            },
            [&](size_t col, int k) {
                const uint8_t m = grid[col * static_cast<size_t>(nz) + k];
                if (lut[m] != 0) {
                    exposed[col * static_cast<size_t>(k_lim) + k] |= 2u;
                }
            }
        );
    }

    if (min_eval_overall >= k_lim) return 0;

    int64_t removed = 0;
    #pragma omp parallel for schedule(static) reduction(+:removed)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        const uint8_t* e = exposed.get() + static_cast<size_t>(col) * k_lim;
        int64_t local = 0;
        for (int k = min_eval_overall; k < k_lim; ++k) {
            if ((e[k] & 2u) != 0u) {
                g[k] = 0;
                ++local;
            }
        }
        removed += local;
    }
    return removed;
}

int64_t voxel_widen_2d_clear_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    int n_groups,
    const double* lat_radii_um,
    const uint8_t* group_mids,
    double dx,
    double dy
) {
    const int k_lim = std::min(nz, std::max(0, ktop));
    if (k_lim <= 0 || n_groups <= 0) return 0;

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    std::unique_ptr<uint8_t[]> exposed(new uint8_t[n_cols * static_cast<size_t>(k_lim)]);
    const int min_exp_k = compute_exposed_void_mask_internal(
        grid, nx, ny, nz, k_lim, exposed.get()
    );
    if (min_exp_k >= k_lim) return 0;

    for (int g_idx = 0; g_idx < n_groups; ++g_idx) {
        const double lat_um = lat_radii_um[g_idx];
        const uint8_t target_mid = group_mids[g_idx];
        const int rx = static_cast<int>(std::llround(lat_um / dx));
        const int ry = static_cast<int>(std::llround(lat_um / dy));
        if (rx <= 0 && ry <= 0) continue;

        const double r_tol_sq = lat_um * lat_um + 1e-15;
        const double r_tol = std::sqrt(r_tol_sq);
        const float r_tol_sq_f = static_cast<float>(r_tol_sq);
        const double cap_inf = 4.0 * r_tol_sq + 1.0;
        const float cap_inf_f = static_cast<float>(cap_inf);

        #pragma omp parallel
        {
            std::vector<float> slice(n_cols);
            EdtThreadWorkspace ws;
            #pragma omp for schedule(dynamic, 2)
            for (int k = min_exp_k; k < k_lim; ++k) {
                bool any_gas = false;
                bool any_target = false;
                for (size_t c = 0; c < n_cols; ++c) {
                    const bool is_gas = (exposed[c * static_cast<size_t>(k_lim) + k] & 1u) != 0u;
                    slice[c] = is_gas ? 0.0f : cap_inf_f;
                    if (is_gas) any_gas = true;
                    if (grid[c * static_cast<size_t>(nz) + k] == target_mid) any_target = true;
                }
                if (!any_gas || !any_target) continue;
                edt_2d_slice_inplace(slice.data(), nx, ny, dx, dy, r_tol, cap_inf, ws);
                for (size_t c = 0; c < n_cols; ++c) {
                    if (slice[c] <= r_tol_sq_f && grid[c * static_cast<size_t>(nz) + k] == target_mid) {
                        exposed[c * static_cast<size_t>(k_lim) + k] |= 2u;
                    }
                }
            }
        }
    }

    int64_t removed = 0;
    #pragma omp parallel for schedule(static) reduction(+:removed)
    for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        const uint8_t* e = exposed.get() + static_cast<size_t>(col) * k_lim;
        int64_t local = 0;
        for (int k = min_exp_k; k < k_lim; ++k) {
            if ((e[k] & 2u) != 0u) {
                g[k] = 0;
                ++local;
            }
        }
        removed += local;
    }
    return removed;
}

void voxel_rasterize_polygons(
    int nx,
    int ny,
    double dx,
    double dy,
    int n_polys,
    const int32_t* poly_offsets,
    const double* vertices_xy,
    uint8_t* out_mask_2d
) {
    std::memset(out_mask_2d, 0, static_cast<size_t>(nx) * static_cast<size_t>(ny));
    if (n_polys <= 0) return;

    #pragma omp parallel for schedule(static)
    for (int ix = 0; ix < nx; ++ix) {
        const double x = (static_cast<double>(ix) + 0.5) * dx + 1.37e-9;
        uint8_t* row = out_mask_2d + static_cast<size_t>(ix) * ny;
        for (int iy = 0; iy < ny; ++iy) {
            const double y = (static_cast<double>(iy) + 0.5) * dy + 2.19e-9;
            bool hit = false;
            for (int p = 0; p < n_polys && !hit; ++p) {
                const int start = poly_offsets[p];
                const int end = poly_offsets[p + 1];
                const int n_verts = end - start;
                if (n_verts < 3) continue;
                bool inside = false;
                for (int idx = 0; idx < n_verts; ++idx) {
                    const int i0 = start + idx;
                    const int i1 = start + ((idx + 1 == n_verts) ? 0 : (idx + 1));
                    const double x0 = vertices_xy[2 * i0];
                    const double y0 = vertices_xy[2 * i0 + 1];
                    const double x1 = vertices_xy[2 * i1];
                    const double y1 = vertices_xy[2 * i1 + 1];
                    if ((y0 > y) != (y1 > y)) {
                        const double x_int = x0 + (y - y0) * (x1 - x0) / (y1 - y0);
                        if (x < x_int) {
                            inside = !inside;
                        }
                    }
                }
                if (inside) hit = true;
            }
            row[iy] = hit ? 1 : 0;
        }
    }
}

int64_t voxel_litho_coat_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    const uint8_t* in_poly_2d,
    int k0,
    int k1,
    uint8_t mid,
    int negative_polarity
) {
    k0 = std::max(0, std::min(nz, k0));
    k1 = std::max(0, std::min(nz, k1));
    if (k1 <= k0) return 0;

    const size_t n_cols = static_cast<size_t>(nx) * static_cast<size_t>(ny);
    int64_t placed = 0;

    if (negative_polarity) {
        std::unique_ptr<uint8_t[]> exposed(new uint8_t[n_cols * static_cast<size_t>(k1)]);
        compute_exposed_void_mask_internal(grid, nx, ny, nz, k1, exposed.get());

        #pragma omp parallel for schedule(static) reduction(+:placed)
        for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
            if (in_poly_2d[col] != 0) continue;
            uint8_t* g = grid + static_cast<size_t>(col) * nz;
            const uint8_t* e = exposed.get() + static_cast<size_t>(col) * k1;
            int64_t local = 0;
            for (int k = k0; k < k1; ++k) {
                if (e[k] != 0) {
                    g[k] = mid;
                    ++local;
                }
            }
            placed += local;
        }
    } else {
        #pragma omp parallel for schedule(static) reduction(+:placed)
        for (ptrdiff_t col = 0; col < static_cast<ptrdiff_t>(n_cols); ++col) {
            if (in_poly_2d[col] == 0) continue;
            uint8_t* g = grid + static_cast<size_t>(col) * nz;
            int64_t local = 0;
            for (int k = k0; k < k1; ++k) {
                if (g[k] == 0) {
                    g[k] = mid;
                    ++local;
                }
            }
            placed += local;
        }
    }
    return placed;
}

int64_t voxel_strip_materials_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int ktop,
    const uint8_t* strip_lut_256
) {
    const int k_lim = std::min(nz, std::max(0, ktop));
    if (k_lim <= 0) return 0;
    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    int64_t removed = 0;

    #pragma omp parallel for schedule(static) reduction(+:removed)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        int64_t local = 0;
        for (int k = 0; k < k_lim; ++k) {
            if (strip_lut_256[g[k]] != 0) {
                g[k] = 0;
                ++local;
            }
        }
        removed += local;
    }
    return removed;
}

int64_t voxel_cmp_clear_above_inplace(
    uint8_t* grid,
    int nx,
    int ny,
    int nz,
    int k_cut,
    int ktop
) {
    k_cut = std::max(0, std::min(nz, k_cut));
    ktop = std::max(0, std::min(nz, ktop));
    if (ktop <= k_cut) return 0;

    const ptrdiff_t n_cols = static_cast<ptrdiff_t>(nx) * static_cast<ptrdiff_t>(ny);
    int64_t removed = 0;

    #pragma omp parallel for schedule(static) reduction(+:removed)
    for (ptrdiff_t col = 0; col < n_cols; ++col) {
        uint8_t* g = grid + static_cast<size_t>(col) * nz;
        int64_t local = 0;
        for (int k = k_cut; k < ktop; ++k) {
            if (g[k] != 0) {
                g[k] = 0;
                ++local;
            }
        }
        removed += local;
    }
    return removed;
}

}  // extern "C"
