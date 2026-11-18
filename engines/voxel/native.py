"""On-demand compiler and ctypes bindings for ``engines/voxel/_native.cpp``.

Compiles ``_native.cpp`` once into ``__pycache__/_voxel_native_<sha256[:12]>.so`` with
``g++ -O3 -fopenmp -shared -fPIC`` (~0.4 s) and loads it into the process. Falls back
cleanly to ``None`` if no C++ toolchain is present so pure-NumPy environments continue
to work unmodified.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np

_HERE = Path(__file__).resolve().parent
_CPP_SOURCE = _HERE / "_native.cpp"
_CACHE_DIR = _HERE / "__pycache__"

_U8_PTR = ctypes.POINTER(ctypes.c_uint8)
_I32_PTR = ctypes.POINTER(ctypes.c_int32)
_F64_PTR = ctypes.POINTER(ctypes.c_double)


def _configure_signatures(lib: ctypes.CDLL) -> ctypes.CDLL:
    lib.voxel_fill_z_band.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint8,
    ]
    lib.voxel_fill_z_band.restype = None

    lib.voxel_active_k_top.argtypes = [_U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.voxel_active_k_top.restype = ctypes.c_int

    lib.voxel_column_tops_k.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _I32_PTR,
    ]
    lib.voxel_column_tops_k.restype = None

    lib.voxel_material_max_k.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint8,
    ]
    lib.voxel_material_max_k.restype = ctypes.c_int

    lib.voxel_exposed_void_mask.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _U8_PTR,
    ]
    lib.voxel_exposed_void_mask.restype = None

    lib.voxel_dilate_3d.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        _U8_PTR,
    ]
    lib.voxel_dilate_3d.restype = None

    lib.voxel_depo_fill_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint8,
    ]
    lib.voxel_depo_fill_inplace.restype = ctypes.c_int64

    lib.voxel_depo_isotropic_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint8,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ]
    lib.voxel_depo_isotropic_inplace.restype = ctypes.c_int64

    lib.voxel_depo_directional_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint8,
    ]
    lib.voxel_depo_directional_inplace.restype = ctypes.c_int64

    lib.voxel_top_down_etch_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _F64_PTR, ctypes.c_double, ctypes.c_double,
    ]
    lib.voxel_top_down_etch_inplace.restype = ctypes.c_int64

    lib.voxel_etch_isotropic_substep_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, _F64_PTR, _U8_PTR,
        ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ]
    lib.voxel_etch_isotropic_substep_inplace.restype = ctypes.c_int64

    lib.voxel_widen_2d_clear_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, _F64_PTR, _U8_PTR,
        ctypes.c_double, ctypes.c_double,
    ]
    lib.voxel_widen_2d_clear_inplace.restype = ctypes.c_int64

    lib.voxel_rasterize_polygons.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_double,
        ctypes.c_int, _I32_PTR, _F64_PTR, _U8_PTR,
    ]
    lib.voxel_rasterize_polygons.restype = None

    lib.voxel_litho_coat_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_uint8, ctypes.c_int,
    ]
    lib.voxel_litho_coat_inplace.restype = ctypes.c_int64

    lib.voxel_strip_materials_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _U8_PTR,
    ]
    lib.voxel_strip_materials_inplace.restype = ctypes.c_int64

    lib.voxel_cmp_clear_above_inplace.argtypes = [
        _U8_PTR, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    lib.voxel_cmp_clear_above_inplace.restype = ctypes.c_int64
    return lib


@lru_cache(maxsize=1)
def get_lib() -> ctypes.CDLL | None:
    if os.environ.get("VIENNAPS_VOXEL_DISABLE_NATIVE") == "1":
        return None
    if not _CPP_SOURCE.is_file():
        return None
    digest = hashlib.sha256(_CPP_SOURCE.read_bytes()).hexdigest()[:12]
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    so_path = _CACHE_DIR / f"_voxel_native_{digest}.so"

    if not so_path.is_file():
        cxx = shutil.which("g++") or shutil.which("c++")
        if not cxx:
            return None
        tmp_so = _CACHE_DIR / f"_voxel_native_{digest}.{os.getpid()}.tmp.so"
        cmd = [
            cxx,
            "-O3",
            "-std=c++17",
            "-fopenmp",
            "-shared",
            "-fPIC",
            str(_CPP_SOURCE),
            "-o",
            str(tmp_so),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            tmp_so.unlink(missing_ok=True)
            return None
        tmp_so.replace(so_path)

    try:
        return _configure_signatures(ctypes.CDLL(str(so_path)))
    except OSError:
        return None


def is_available() -> bool:
    return get_lib() is not None


def _check_grid(grid: np.ndarray) -> tuple[int, int, int, _U8_PTR]:
    if grid.dtype != np.uint8 or grid.ndim != 3 or not grid.flags["C_CONTIGUOUS"]:
        raise ValueError("VoxelState.grid must be a C-contiguous uint8 3D array")
    nx, ny, nz = grid.shape
    return int(nx), int(ny), int(nz), grid.ctypes.data_as(_U8_PTR)


def fill_z_band(grid: np.ndarray, ka: int, kb: int, mid: int) -> None:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    lib.voxel_fill_z_band(ptr, nx, ny, nz, int(ka), int(kb), int(mid))


def active_k_top(grid: np.ndarray) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(lib.voxel_active_k_top(ptr, nx, ny, nz))


def column_tops_k(grid: np.ndarray, ktop: int) -> np.ndarray:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    out = np.empty((nx, ny), dtype=np.int32)
    lib.voxel_column_tops_k(ptr, nx, ny, nz, int(ktop), out.ctypes.data_as(_I32_PTR))
    return out


def material_max_k(grid: np.ndarray, ktop: int, mid: int) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(lib.voxel_material_max_k(ptr, nx, ny, nz, int(ktop), int(mid)))


def exposed_void_mask(grid: np.ndarray, k_limit: int) -> np.ndarray:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    k_lim = max(0, min(nz, int(k_limit)))
    out = np.empty((nx, ny, k_lim), dtype=np.uint8)
    if k_lim > 0:
        lib.voxel_exposed_void_mask(ptr, nx, ny, nz, k_lim, out.ctypes.data_as(_U8_PTR))
    return out.view(np.bool_)


def dilate_3d(
    mask: np.ndarray, radius_um: float, dx: float, dy: float, dz: float
) -> np.ndarray:
    lib = get_lib()
    assert lib is not None
    src = np.ascontiguousarray(mask.view(np.uint8) if mask.dtype == np.bool_ else mask.astype(np.uint8))
    nx, ny, kz = src.shape
    out = np.empty((nx, ny, kz), dtype=np.uint8)
    lib.voxel_dilate_3d(
        src.ctypes.data_as(_U8_PTR),
        int(nx),
        int(ny),
        int(kz),
        float(radius_um),
        float(dx),
        float(dy),
        float(dz),
        out.ctypes.data_as(_U8_PTR),
    )
    return out.view(np.bool_)


def depo_fill_inplace(grid: np.ndarray, k_target: int, mid: int) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(lib.voxel_depo_fill_inplace(ptr, nx, ny, nz, int(k_target), int(mid)))


def depo_isotropic_inplace(
    grid: np.ndarray,
    k_lim: int,
    mid: int,
    thickness_um: float,
    dx: float,
    dy: float,
    dz: float,
) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(
        lib.voxel_depo_isotropic_inplace(
            ptr, nx, ny, nz, int(k_lim), int(mid),
            float(thickness_um), float(dx), float(dy), float(dz),
        )
    )


def depo_directional_inplace(grid: np.ndarray, ktop: int, dk: int, mid: int) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(lib.voxel_depo_directional_inplace(ptr, nx, ny, nz, int(ktop), int(dk), int(mid)))


def top_down_etch_inplace(
    grid: np.ndarray,
    ktop: int,
    rate_by_id: dict[int, float],
    duration: float,
    dz: float,
) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    lut = np.zeros(256, dtype=np.float64)
    for mid, rate in rate_by_id.items():
        if 0 < int(mid) < 256 and float(rate) > 0.0:
            lut[int(mid)] = float(rate)
    return int(
        lib.voxel_top_down_etch_inplace(
            ptr, nx, ny, nz, int(ktop), lut.ctypes.data_as(_F64_PTR), float(duration), float(dz),
        )
    )


def etch_isotropic_substep_inplace(
    grid: np.ndarray,
    ktop: int,
    rate_by_id: dict[int, float],
    dt: float,
    dx: float,
    dy: float,
    dz: float,
) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    # Group materials sharing an identical displacement radius in this sub-step
    by_radius: dict[float, list[int]] = {}
    for mid, rate in rate_by_id.items():
        radius = round(float(rate) * float(dt), 12)
        if radius > 0.0 and 0 < int(mid) < 256:
            by_radius.setdefault(radius, []).append(int(mid))
    if not by_radius:
        return 0
    radii = np.array(sorted(by_radius.keys()), dtype=np.float64)
    luts = np.zeros((len(radii), 256), dtype=np.uint8)
    for idx, r in enumerate(radii):
        for mid in by_radius[float(r)]:
            luts[idx, mid] = 1
    return int(
        lib.voxel_etch_isotropic_substep_inplace(
            ptr, nx, ny, nz, int(ktop),
            len(radii),
            radii.ctypes.data_as(_F64_PTR),
            luts.ctypes.data_as(_U8_PTR),
            float(dx), float(dy), float(dz),
        )
    )


def widen_2d_clear_inplace(
    grid: np.ndarray,
    ktop: int,
    l_rates: dict[int, float],
    duration: float,
    dx: float,
    dy: float,
) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    mids_list: list[int] = []
    radii_list: list[float] = []
    for mid, l_rate in l_rates.items():
        lat_um = float(l_rate) * float(duration) * 0.5
        if lat_um > 0.0 and 0 < int(mid) < 256:
            mids_list.append(int(mid))
            radii_list.append(lat_um)
    if not mids_list:
        return 0
    radii = np.array(radii_list, dtype=np.float64)
    mids = np.array(mids_list, dtype=np.uint8)
    return int(
        lib.voxel_widen_2d_clear_inplace(
            ptr, nx, ny, nz, int(ktop),
            len(mids_list),
            radii.ctypes.data_as(_F64_PTR),
            mids.ctypes.data_as(_U8_PTR),
            float(dx), float(dy),
        )
    )


def rasterize_polygons(
    nx: int,
    ny: int,
    dx: float,
    dy: float,
    polygons: Sequence[Sequence[tuple[float, float]]],
) -> np.ndarray:
    lib = get_lib()
    assert lib is not None
    out = np.zeros((nx, ny), dtype=np.uint8)
    offsets = [0]
    verts: list[float] = []
    for poly in polygons:
        ring = poly[:-1] if len(poly) > 3 and poly[0] == poly[-1] else poly
        if len(ring) < 3:
            continue
        for x, y in ring:
            verts.append(float(x))
            verts.append(float(y))
        offsets.append(len(verts) // 2)
    n_polys = len(offsets) - 1
    if n_polys == 0:
        return out.view(np.bool_)
    off_arr = np.asarray(offsets, dtype=np.int32)
    v_arr = np.asarray(verts, dtype=np.float64)
    lib.voxel_rasterize_polygons(
        int(nx), int(ny), float(dx), float(dy),
        n_polys, off_arr.ctypes.data_as(_I32_PTR), v_arr.ctypes.data_as(_F64_PTR),
        out.ctypes.data_as(_U8_PTR),
    )
    return out.view(np.bool_)


def litho_coat_inplace(
    grid: np.ndarray,
    in_poly: np.ndarray,
    k0: int,
    k1: int,
    mid: int,
    negative: bool,
) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    poly_u8 = np.ascontiguousarray(
        in_poly.view(np.uint8) if in_poly.dtype == np.bool_ else in_poly.astype(np.uint8)
    )
    return int(
        lib.voxel_litho_coat_inplace(
            ptr, nx, ny, nz,
            poly_u8.ctypes.data_as(_U8_PTR),
            int(k0), int(k1), int(mid), 1 if negative else 0,
        )
    )


def strip_materials_inplace(grid: np.ndarray, ktop: int, mids: Sequence[int]) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    lut = np.zeros(256, dtype=np.uint8)
    for m in mids:
        if 0 < int(m) < 256:
            lut[int(m)] = 1
    return int(lib.voxel_strip_materials_inplace(ptr, nx, ny, nz, int(ktop), lut.ctypes.data_as(_U8_PTR)))


def cmp_clear_above_inplace(grid: np.ndarray, k_cut: int, ktop: int) -> int:
    lib = get_lib()
    assert lib is not None
    nx, ny, nz, ptr = _check_grid(grid)
    return int(lib.voxel_cmp_clear_above_inplace(ptr, nx, ny, nz, int(k_cut), int(ktop)))
