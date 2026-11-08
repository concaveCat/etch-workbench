"""Native GDS layer bodies, padding, optional proximity blur and fallbacks.

The product domain owns one level set per material, and ViennaPS requires every
level set to contain all lower materials.  GDS bodies must therefore be built on
the shared GDS grid and inserted with the native cumulative union; appending a
standalone body makes the top level set contain only that body and Advect then
clips earlier materials away.

The installed 4.6.2 build has two measured quirks:

* ``d3.GDSGeometry.layerToLevelSet`` deterministically returns an empty level
  set for some valid ``baseHeight``/``height`` pairs (for example base 0.2 /
  height 0.4 at gridDelta 0.2).  ``worker/simulate.py`` prefers the native body
  when it is non-empty and falls back to the native 2D read plus ViennaLS
  extrusion/caps on the same grid otherwise; an empty body is never extruded.
* A polygon that fills the layout frame has no inside/outside pair, so its 2D
  interior reads back empty.  ``setBoundaryPadding`` widens the native frame
  (one grid cell in production) without changing any polygon; an empty interior
  is never passed to ``Extrude`` (SIGSEGV).

``addBlur`` is the installed GDS proximity blur (Gaussian sigmas, normalized
weights and one threshold contour), not a resist or OPC model.

The module deliberately does not touch the domain, read files, register
materials or import the top-level ``viennaps`` package; the caller owns all of
that.
"""

from __future__ import annotations

import math
from typing import Any

CONSTRUCTION = "native-extrude-capped-surface-regrid-union-v1"
NATIVE_CONSTRUCTION = "native-layer-to-levelset-v1"
NATIVE_REGRID_CONSTRUCTION = "native-layer-to-levelset-regrid-union-v1"
EMPTY_MASK_ERROR = "GDS 掩膜在当前计算域内为空，请调整版图位置、保留区域或计算域。"
EMPTY_PLANAR_ERROR = (
    "GDS 图层 {layer} 在当前 GDS 边界内没有可用的多边形内部（多边形贴合库边界、"
    "小于网格间距或退化），无法构造实体。请让该图层多边形相对 GDS 库边界内缩至少"
    "一个 gridDelta，或减小 gridDelta 后重试。"
)
_SURFACE_TOLERANCE = 0.01


def planar_point_count(planar: Any) -> int:
    """Public readback of the native 2D polygon interior point count."""
    return int(planar.getNumberOfPoints())


def _point_count(level_set: Any) -> int:
    return int(level_set.getNumberOfPoints())


def resolve_blur(blur: Any, grid_delta: float) -> dict[str, Any] | None:
    """Validate and resolve one ``geometry.blur`` block for the native API.

    ``sigmas`` follow the installed native convention: they are expressed in
    grid intervals, so the physical Gaussian is ``sigma * gridDelta`` um. This
    function never mutates the request and returns ``None`` when the block is
    absent or disabled.
    """
    if blur is None:
        return None
    if not isinstance(blur, dict):
        raise RuntimeError("geometry.blur must be an object")
    allowed = {"enabled", "sigmas", "weights", "threshold", "delta", "gridRefine"}
    unknown = sorted(set(blur) - allowed)
    if unknown:
        raise RuntimeError(f"geometry.blur contains unknown keys: {', '.join(unknown)}")
    if not bool(blur.get("enabled", True)):
        return None
    sigmas = blur.get("sigmas")
    weights = blur.get("weights")
    if not isinstance(sigmas, list) or not isinstance(weights, list):
        raise RuntimeError("geometry.blur requires sigmas and weights arrays")
    if not sigmas or len(sigmas) > 4 or len(weights) > 4:
        raise RuntimeError("geometry.blur accepts one to four sigmas and weights")
    if len(sigmas) != len(weights):
        raise RuntimeError("geometry.blur sigmas and weights must have the same length")
    values = [float(value) for value in sigmas]
    weight_values = [float(value) for value in weights]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise RuntimeError("geometry.blur sigmas must be finite and positive")
    if any(not math.isfinite(value) or value < 0 for value in weight_values):
        raise RuntimeError("geometry.blur weights must be finite and non-negative")
    total_weight = 0.0
    for weight in weight_values:
        total_weight += weight
        if not math.isfinite(total_weight):
            raise RuntimeError("geometry.blur weights sum exceeds the finite range")
    if total_weight <= 0:
        raise RuntimeError("geometry.blur weights must have a positive sum")
    threshold = float(blur.get("threshold", 0.5))
    if not math.isfinite(threshold) or not 0.0 < threshold < 1.0:
        raise RuntimeError("geometry.blur threshold must be strictly between 0 and 1")
    delta = blur.get("delta")
    exposure_delta = float(grid_delta) if delta is None else float(delta)
    if (not math.isfinite(exposure_delta) or exposure_delta <= 0
            or exposure_delta > float(grid_delta) + 1e-12):
        raise RuntimeError(
            "geometry.blur.delta is the exposure grid spacing and must be finite, "
            "positive and not coarser than gridDelta")
    grid_refine = blur.get("gridRefine", 4)
    if type(grid_refine) is not int or not 1 <= grid_refine <= 8:
        raise RuntimeError("geometry.blur.gridRefine must be an integer from 1 to 8")
    ratio = max(1, math.ceil(grid_refine * exposure_delta / float(grid_delta)))
    return {
        "sigmas": values,
        "weights": weight_values,
        "threshold": threshold,
        "delta": exposure_delta,
        "gridRefine": grid_refine,
        "gridRefinement": ratio,
        "physicalSigmasUm": [value * float(grid_delta) for value in values],
    }


def apply_gds_blur(geometry: Any, blur: dict[str, Any] | None) -> None:
    """Configure the installed native ``GDSGeometry.addBlur`` once per reader."""
    if blur is None:
        return
    geometry.addBlur(list(blur["sigmas"]), list(blur["weights"]),
                     float(blur["threshold"]), float(blur["delta"]),
                     int(blur["gridRefine"]))


def native_layer_body(geometry: Any, layer: Any, *, base_height: float,
                      height: float, mask_tone: bool = False,
                      blur_layer: bool = True) -> Any | None:
    """Return the native 3D body or ``None`` when the installed build is empty.

    ViennaPS 4.6.2's ``d3.GDSGeometry.layerToLevelSet`` deterministically
    returns an empty level set for some valid ``baseHeight``/``height`` pairs
    (for example base 0.2/height 0.4 at gridDelta 0.2). The caller keeps the
    fully native 2D-read + ViennaLS extrusion fallback for that case; an empty
    body must never be inserted or extruded.
    """
    body = geometry.layerToLevelSet(int(layer), float(round(float(base_height), 12)),
                                    float(height), bool(mask_tone), bool(blur_layer))
    if int(body.getNumberOfPoints()) == 0:
        return None
    return body


def require_planar_interior(planar: Any, layer: Any) -> None:
    """Reject an empty 2D interior before it reaches the crashing native Extrude."""
    if _point_count(planar) == 0:
        raise RuntimeError(EMPTY_PLANAR_ERROR.format(layer=layer))


def _lower_half_space(ps: Any, template: Any, height: float) -> Any:
    """Copy ``template`` and replace it with the lower half space at ``height``."""
    height = round(float(height), 12)
    half_space = ps.ls.d3.Domain(template)
    ps.ls.d3.MakeGeometry(
        half_space, ps.ls.d3.Plane([0.0, 0.0, height], [0.0, 0.0, 1.0])).apply()
    return half_space


def _finite_source_slab(ps: Any, template: Any, base_height: float, top_height: float) -> Any:
    """Finite slab between ``base_height`` and ``top_height`` on ``template``'s grid."""
    upper = _lower_half_space(ps, template, top_height)
    lower = _lower_half_space(ps, template, base_height)
    ps.ls.d3.BooleanOperation(
        upper, lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    return upper


def _finite_target_slab(ps: Any, factory: Any, base_height: float, top_height: float) -> Any:
    """Finite slab between two substrate half spaces on the factory's grid."""
    upper = factory.makeSubstrate(top_height)
    lower = factory.makeSubstrate(base_height)
    ps.ls.d3.BooleanOperation(
        upper, lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    return upper


def _capped_extrusion(ps: Any, domain: Any, planar: Any, base_height: float,
                      top_height: float, boundaries: list[Any] | None = None,
                      *, layer: Any = None) -> tuple[Any, dict[str, int]]:
    """Extrude ``planar`` to a finite body with closed top/bottom caps."""
    # ViennaLS Extrude dereferences the input domain's grid points; an empty
    # planar field (a polygon that fills the GDS bounds has no zero crossing)
    # crashes the process instead of raising.  Fail explicitly instead.
    require_planar_interior(planar, layer if layer is not None else "<unknown>")
    reflective = ps.BoundaryType.REFLECTIVE_BOUNDARY
    if boundaries is None:
        # Stack masks are regridded onto the simulation grid later, so the
        # intermediate extrusion stays decoupled from the final periodicity.
        boundaries = [reflective, reflective, ps.BoundaryType.INFINITE_BOUNDARY]
    extruded = ps.ls.d3.Domain(float(domain.getSetup().gridDelta()))
    ps.ls.Extrude(planar, extruded, [base_height, top_height], 2, boundaries).apply()
    extruded_points = _point_count(extruded)

    # Without explicit caps the sparse planar field leaves long open runs, which
    # breaks full-domain and large-polygon cases after regridding.
    source_slab = _finite_source_slab(ps, extruded, base_height, top_height)
    ps.ls.d3.BooleanOperation(
        extruded, source_slab, ps.ls.BooleanOperationEnum.INTERSECT).apply()
    return extruded, {"extrudedPointCount": extruded_points}


def _surface(ps: Any, body: Any) -> Any:
    surface = ps.ls.Mesh()
    ps.ls.d3.ToSurfaceMesh(ps.ls.d3.Domain(body), surface, _SURFACE_TOLERANCE).apply()
    return surface


def _regrid(ps: Any, domain: Any, surface: Any) -> Any:
    """Reconstruct a capped body on the real simulation grid."""
    aligned = ps.ls.d3.Domain(domain.getGrid())
    reconstruction = ps.ls.d3.FromSurfaceMesh(aligned, surface)
    reconstruction.setRemoveBoundaryTriangles(False)
    reconstruction.apply()
    return aligned


def _capped_and_regrid(ps: Any, domain: Any, planar: Any, base_height: float,
                       top_height: float, *, layer: Any = None) -> tuple[Any, dict[str, int]]:
    extruded, stats = _capped_extrusion(
        ps, domain, planar, base_height, top_height, layer=layer)
    surface = _surface(ps, extruded)
    stats["sourceSurfacePointCount"] = len(surface.getNodes())
    stats["sourceSurfaceTriangleCount"] = len(surface.getTriangles())
    return _regrid(ps, domain, surface), stats


def build_aligned_gds_layer(ps: Any, domain: Any, planar_inside: Any, *, base_height: float,
                            height: float, mask_tone: bool = False,
                            layer: Any = None,
                            native_body: Any | None = None) -> tuple[Any, dict[str, Any]]:
    """Return one generic GDS layer body plus diagnostics.

    When ``native_body`` is given it is the installed 3D
    ``layerToLevelSet`` output (already on the shared GDS grid) and is used
    unchanged. The hand construction below stays as the measured fallback for
    the installed build's deterministic empty-body cases.

    ``planar_inside`` is the native 2D polygon interior.  The body stays on the
    shared GDS grid, matching the native layer grid; every mapping uses the same
    grid, so the caller can wrap cumulatively.  ``mask_tone`` keeps the installed
    ``layerToLevelSet(mask=True)`` semantics: a finite slab over the GDS bounds
    with the polygons removed.  Building that slab on the extruded body's native
    grid (instead of inverting the sparse 2D field) keeps every surface closed
    for Advect and particle processes.

    ``layer`` only labels error messages and diagnostics.  An empty planar
    interior raises instead of being extruded (which would crash natively).
    """
    base_height = round(float(base_height), 12)
    top_height = round(base_height + float(height), 12)
    if native_body is not None and _point_count(native_body):
        # Native 3D output is already the finite, capped body on the GDS grid.
        # Keeping it untouched preserves the native mask tone and layer grid.
        diagnostics: dict[str, Any] = {
            "gridSource": "gds-layer-grid",
            "construction": NATIVE_CONSTRUCTION,
            "maskTone": bool(mask_tone),
            "planarPointCount": 0 if planar_inside is None else _point_count(planar_inside),
            "nativeBodyPointCount": _point_count(native_body),
            "alignedLayerPointCount": _point_count(native_body),
        }
        return native_body, diagnostics
    require_planar_interior(planar_inside, layer if layer is not None else "<unknown>")
    # Generic layers are inserted on their own GDS grid, so they must carry the
    # simulation domain's lateral boundary conditions (periodic stays periodic).
    extruded, stats = _capped_extrusion(
        ps, domain, planar_inside, base_height, top_height,
        list(domain.getSetup().boundaryCons()), layer=layer)
    body = extruded
    if mask_tone:
        target_slab = _finite_source_slab(ps, extruded, base_height, top_height)
        if _point_count(extruded):
            ps.ls.d3.BooleanOperation(
                target_slab, extruded, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        body = target_slab
    surface = _surface(ps, body)
    stats["sourceSurfacePointCount"] = len(surface.getNodes())
    stats["sourceSurfaceTriangleCount"] = len(surface.getTriangles())
    # Generic GDS layers stay on their native GDS grid, exactly like the native
    # layerToLevelSet output.  All mappings share that grid, so the cumulative
    # union is safe; clipping to the user domain is reserved for the stack mask.
    aligned = body
    points = _point_count(aligned)
    diagnostics = {
        "gridSource": "gds-layer-grid",
        "construction": CONSTRUCTION,
        "nativeBodyPointCount": 0,
        "maskTone": bool(mask_tone),
        "planarPointCount": _point_count(planar_inside),
        "alignedLayerPointCount": points,
        **stats,
    }
    if points == 0:
        raise RuntimeError("GDS 图层在当前计算域内为空，请调整版图位置或计算域。")
    return aligned, diagnostics


def build_aligned_gds_mask(ps: Any, domain: Any, planar_inside: Any, *, base_height: float,
                           height: float, retained_region: str,
                           layer: Any = None,
                           native_body: Any | None = None) -> tuple[Any, dict[str, Any]]:
    """Return a mask level set on ``domain``'s grid plus construction diagnostics.

    ``planar_inside`` must be the polygon interior read from the native 2D
    ``GDSGeometry.layerToLevelSet`` call (not inverted).  ``retained_region`` is
    ``"inside"`` or ``"outside"``.  No material is inserted into ``domain``.
    ``layer`` only labels error messages, so an empty interior never reaches
    the crashing native Extrude.

    ``native_body`` is the native 3D *inside* body on the GDS grid. It is
    regridded onto the simulation grid, which is still required for a boolean
    operand or a mask union; the fallback builds that body from the 2D interior.
    """
    if retained_region not in {"inside", "outside"}:
        raise ValueError(f"unsupported GDS retained region: {retained_region!r}")
    base_height = round(float(base_height), 12)
    top_height = round(base_height + float(height), 12)
    if native_body is not None and _point_count(native_body):
        source = native_body
        surface = _surface(ps, source)
        stats = {
            "nativeBodyPointCount": _point_count(source),
            "sourceSurfacePointCount": len(surface.getNodes()),
            "sourceSurfaceTriangleCount": len(surface.getTriangles()),
        }
        aligned = _regrid(ps, domain, surface)
        construction = NATIVE_REGRID_CONSTRUCTION
    else:
        require_planar_interior(planar_inside, layer if layer is not None else "<unknown>")
        aligned, stats = _capped_and_regrid(
            ps, domain, planar_inside, base_height, top_height, layer=layer)
        stats["nativeBodyPointCount"] = 0
        construction = CONSTRUCTION

    diagnostics: dict[str, Any] = {
        "gridSource": "simulation-domain",
        "wrapLowerLevelSet": True,
        "retainedRegion": retained_region,
        "construction": construction,
        "planarPointCount": 0 if planar_inside is None else _point_count(planar_inside),
        **stats,
    }

    # Polarity.  "inside" is the regridded body; "outside" is the finite target
    # slab minus that body, built on the same compatible grid.
    if retained_region == "outside":
        inside_points = _point_count(aligned)
        diagnostics["insideMaskPointCount"] = inside_points
        target_slab = _finite_target_slab(
            ps, ps.GeometryFactory(domain.getSetup(), "GdsMaskComplement"),
            base_height, top_height)
        if inside_points:
            ps.ls.d3.BooleanOperation(
                target_slab, aligned, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        aligned = target_slab

    aligned_points = _point_count(aligned)
    diagnostics["alignedMaskPointCount"] = aligned_points
    # An empty result is an explicit run failure; a full-domain inside mask is
    # valid.  The check runs after polarity so full-coverage outside is empty.
    if aligned_points == 0:
        raise RuntimeError(EMPTY_MASK_ERROR)
    return aligned, diagnostics
