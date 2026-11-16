"""Orbit camera -> SPX free camera.

SPX's ``meas_screenshot`` accepts ``View`` presets (``iso``/``xz``/...) or
``View='free'``. Measured behaviour of ``free``:

* it requires ``CameraFocalPosition`` and ``CameraPosition``, each a
  ``([x, y, z], unit)`` quantity (a bare ``[x, y, z]`` is rejected);
* a camera position exactly above the focal point makes SPX raise
  ``Boost QVM error`` (degenerate up vector), so steep elevations must fall back
  to a preset;
* with ``OrthographicProjection=True`` the projection matches the workbench's
  orthographic mesh view.

This module keeps that mapping pure so it can be unit-tested without SPX.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OrbitCamera:
    azimuth: float
    elevation: float
    zoom: float = 1.0


@dataclass(frozen=True)
class FreeCamera:
    focal: tuple[float, float, float]
    position: tuple[float, float, float]


MAX_ELEVATION = 85.0


def orbit_to_free(centre: tuple[float, float, float], extent: float,
                  camera: OrbitCamera) -> FreeCamera | None:
    """Place a free camera on a sphere around ``centre``.

    Returns ``None`` when the elevation is too steep for SPX's up vector; the
    caller then renders with a ``View`` preset instead.
    """
    if abs(camera.elevation) > MAX_ELEVATION:
        return None
    zoom = camera.zoom if camera.zoom and camera.zoom > 0 else 1.0
    distance = max(extent, 1e-6) * 4.0 / zoom
    azimuth = math.radians(camera.azimuth)
    elevation = math.radians(camera.elevation)
    direction = (
        math.cos(elevation) * math.sin(azimuth),
        math.cos(elevation) * math.cos(azimuth),
        math.sin(elevation),
    )
    position = tuple(centre[i] + direction[i] * distance for i in range(3))
    return FreeCamera(focal=tuple(centre), position=position)
