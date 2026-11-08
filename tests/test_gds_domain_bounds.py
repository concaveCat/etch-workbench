"""Regression: 2D GDS layouts use the absolute [0, xExtent] frame.

Stage 5 authors a 2D layout at positive X (``CELL_A`` spans 0.25..7.5 um in an
8 um domain). ViennaPS builds the 2D Domain centered, so the old check compared
that layout with ``[-xExtent/2, xExtent/2]`` and rejected a valid layout before
the native insert. The worker and backend now accept the absolute frame as well
as the native centered frame that existing GDS fixtures use. 3D stays centered.
"""

from __future__ import annotations

import unittest

from backend.main import app
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body
from tests.test_gds_native_bridge import (blur_block, gds_project,
                                          gds_rectangle_bytes)
from worker.simulate import _require_gds_bounds_inside_domain


class _BoundsGeometry:
    def __init__(self, bounds: list[float]) -> None:
        self._bounds = bounds

    def getBounds(self) -> list[float]:
        return self._bounds


def _project(dimension: int, *, extent: float, delta: float = 0.25) -> dict:
    return {"domain": ({"gridDelta": delta, "xExtent": extent}
                       if dimension == 2 else
                       {"gridDelta": delta, "xExtent": extent, "yExtent": extent})}


class WorkerGdsDomainBounds(unittest.TestCase):
    def test_2d_absolute_layout_is_accepted(self):
        # Native 2D bounds are [xmin, xmax, ymin, ymax, ...]; 7.5 > xExtent/2
        # but still fits the absolute [0, 8] frame.
        bounds = _require_gds_bounds_inside_domain(
            2, _BoundsGeometry([0.2505, 7.5, 0.0, 3.0, 0.0, 0.0]),
            _project(2, extent=8.0), padding=0.0)
        self.assertEqual(bounds[0], 0.2505)

    def test_2d_centered_layout_still_accepted(self):
        _require_gds_bounds_inside_domain(
            2, _BoundsGeometry([-2.0, 2.0, -2.0, 2.0, 0.0, 0.0]),
            _project(2, extent=6.0), padding=0.0)

    def test_2d_layout_outside_both_frames_is_rejected(self):
        with self.assertRaises(RuntimeError) as raised:
            _require_gds_bounds_inside_domain(
                2, _BoundsGeometry([8.5, 10.0, 0.0, 1.0, 0.0, 0.0]),
                _project(2, extent=8.0), padding=0.0)
        self.assertIn("超出当前计算域", str(raised.exception))

    def test_3d_remains_centered(self):
        # A layout at -5..-4 fits the 3D centered frame but not [0, 12].
        _require_gds_bounds_inside_domain(
            3, _BoundsGeometry([-5.0, -4.0, -1.0, 1.0, 0.0, 0.0]),
            _project(3, extent=12.0), padding=0.0)
        with self.assertRaises(RuntimeError):
            _require_gds_bounds_inside_domain(
                3, _BoundsGeometry([5.5, 6.5, -1.0, 1.0, 0.0, 0.0]),
                _project(3, extent=12.0), padding=0.0)


class BackendGdsDomainBounds(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.life.__aexit__(None, None, None)

    async def upload(self, name: str, data: bytes) -> str:
        response = await asgi_request(
            f"/api/gds-assets?file_name={name}", method="POST", payload=data,
            headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        return json_body(response)["assetId"]

    def project(self, dimension: int, asset_id: str, *, extent: float,
                position: list[float], blur: dict | None = None) -> dict:
        layers = ([{"layer": 1, "material": "Si"}] if dimension == 2 else
                  [{"layer": 1, "material": "Si", "baseHeight": 0.0,
                    "height": 0.5, "mask": False}])
        value = gds_project(dimension, asset_id, extent=extent, layers=layers,
                            structure="RECT", blur=blur)
        value["geometry"]["position"] = position
        return value

    async def test_2d_absolute_layout_accepted_and_outside_rejected(self):
        asset_id = await self.upload("absolute-cell.gds",
                                     gds_rectangle_bytes(7_500, 1_000))
        # Raw X -3.75..3.75 plus position 4 -> 0.25..7.75, inside [0, 8].
        accepted = self.project(2, asset_id, extent=8.0, position=[4.0, 0.0, 0.0])
        validate_project(accepted, kind="preview")
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=accepted)
        self.assertEqual(response[0], 200, response[2])

        outside_asset = await self.upload("outside-cell.gds",
                                          gds_rectangle_bytes(2_000, 1_000))
        outside = self.project(2, outside_asset, extent=8.0,
                               position=[8.0, 0.0, 0.0])
        with self.assertRaises(ProjectInputError):
            validate_project(outside, kind="preview")
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "preview", "project": outside})
        self.assertEqual(response[0], 422, response[2])

    async def test_2d_blur_padding_is_still_enforced(self):
        asset_id = await self.upload("padded-cell.gds",
                                     gds_rectangle_bytes(7_800, 1_000))
        # 0.1..7.9 fits [0, 8] before padding, but 2*delta of native blur
        # padding pushes it outside both accepted frames.
        project = self.project(2, asset_id, extent=8.0, position=[4.0, 0.0, 0.0],
                               blur=blur_block())
        project["domain"]["gridDelta"] = 0.1
        with self.assertRaises(ProjectInputError) as raised:
            validate_project(project, kind="preview")
        self.assertIn("geometry.assetId", str(raised.exception))

    async def test_3d_centered_layout_is_unchanged(self):
        asset_id = await self.upload("centered-3d-cell.gds",
                                     gds_rectangle_bytes(2_000, 2_000))
        # -4.5..-3.5 is inside the 3D centered [-6, 6] frame but outside [0, 12].
        project = self.project(3, asset_id, extent=12.0,
                               position=[-4.5, 0.0, 0.0])
        validate_project(project, kind="preview")
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=project)
        self.assertEqual(response[0], 200, response[2])


if __name__ == "__main__":
    unittest.main()
