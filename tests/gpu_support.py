"""Shared opt-in GPU setup for the plugin tests.

A ViennaPS GPU context is process-global and an in-process CUDA Runtime
readback after the context exists can invalidate the next OptiX acceleration
structure build. The GPU test modules share one device/readback instead of
each calling ``cuda_runtime_readback`` again.
"""

from __future__ import annotations

import os
import unittest
from typing import Any

import viennaps as ps

import worker.simulate as simulate
from backend.gpu_devices import cuda_devices

__all__ = ["acquire_gpu"]

_DEVICE: dict[str, Any] | None = None
_READBACK: dict[str, Any] | None = None


def acquire_gpu() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the single visible CUDA device and its verified readback.

    The first caller performs the readback; later test classes reuse it.
    """
    global _DEVICE, _READBACK
    inventory = cuda_devices()
    if inventory.get("status") != "available" or not inventory.get("devices"):
        raise unittest.SkipTest(f"no CUDA device: {inventory.get('reason')}")

    if _DEVICE is None:
        device = dict(inventory["devices"][0])
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device["uuid"])
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        _DEVICE = device
        _READBACK = simulate.cuda_runtime_readback(device)
    assert _READBACK is not None
    return dict(_DEVICE), dict(_READBACK)
