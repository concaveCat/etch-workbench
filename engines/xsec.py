"""Cross-sections of engine frames, drawn without a 3D viewer.

The comparison UI shows a 3D render, which is fine for humans but useless for an
audit: a resist coat with holes and a set of resist discs look almost the same at a
glancing angle.  Slicing a frame mesh with one vertical plane gives an unambiguous
picture (a hole is a gap in an interface), and it is cheap enough to render all 88
steps of a run into one contact sheet.

Usage::

    python -m engines.xsec var/<run>/engines/viennaps/frames/08_*.mesh.json \
        --y 0.37 --out /tmp/s8.png
    python -m engines.xsec --grid var/<run>/engines/viennaps/frames --out /tmp/all.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_COLORS = {
    "Si": "#78909C",
    "SiO2": "#26C6DA",
    "Si3N4": "#C0CA33",
    "PMMA": "#FDD835",
    "SiOC": "#8D6E63",
    "BARC": "#546E7A",
    "Oxide2": "#90A4AE",
    "SiON": "#4DB6AC",
    "TaN": "#FFB300",
    "Cu": "#FF7043",
    "TiN": "#7E57C2",
    "W": "#42A5F5",
    "PolySi": "#A1887F",
    "CESL": "#00897B",
}


def _slice_segments(points: list[list[float]], triangles: list[list[int]], material_ids: list[int],
                    y: float) -> dict[int, list[tuple[float, float, float, float]]]:
    """Intersect every triangle with the plane ``y = const``.

    A triangle crossing the plane gives exactly one segment; the segment is stored
    as (x0, z0, x1, z1) under its material id.
    """
    out: dict[int, list[tuple[float, float, float, float]]] = {}
    for triangle, material in zip(triangles, material_ids):
        a, b, c = (points[index] for index in triangle)
        side = [b[1] - a[1], c[1] - a[1]]
        if (side[0] > 0) == (side[1] > 0) and min(a[1], b[1], c[1]) > y or \
                max(a[1], b[1], c[1]) < y:
            continue
        hits: list[tuple[float, float]] = []
        for p, q in ((a, b), (b, c), (c, a)):
            dy = q[1] - p[1]
            if abs(dy) < 1e-12:
                if abs(p[1] - y) < 1e-12:
                    hits.append((p[0], p[2]))
                    hits.append((q[0], q[2]))
                continue
            t = (y - p[1]) / dy
            if -1e-9 <= t <= 1 + 1e-9:
                hits.append((p[0] + t * (q[0] - p[0]), p[2] + t * (q[2] - p[2])))
        if len(hits) < 2:
            continue
        (x0, z0), (x1, z1) = hits[0], hits[-1]
        if abs(x1 - x0) < 1e-9 and abs(z1 - z0) < 1e-9:
            continue
        out.setdefault(int(material), []).append((x0, z0, x1, z1))
    return out


def render(frame: dict[str, Any], y: float, width: int = 640, height: int = 360,
           z_range: tuple[float, float] | None = None):
    """Draw one cross-section of a frame mesh as a :class:`PIL.Image.Image`."""
    from PIL import Image, ImageDraw

    points = frame["points"]
    triangles = frame["triangles"]
    material_ids = frame["materialIds"]
    names = {int(entry["id"]): entry["name"] for entry in frame.get("materials", [])}
    colors = {int(entry["id"]): entry.get("color") or DEFAULT_COLORS.get(entry["name"], "#999999")
              for entry in frame.get("materials", [])}

    # bbox is [xMin, yMin, zMin, xMax, yMax, zMax]
    x_min, y_min, z_min, x_max, y_max, z_max = frame["bbox"]
    if z_range is None:
        zs = [p[2] for p in points]
        low, high = min(zs), max(zs)
        pad = max((high - low) * 0.08, 1e-3)
        z_range = (low - pad, high + pad)

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def px(x: float) -> float:
        return (x - x_min) / max(x_max - x_min, 1e-9) * (width - 1)

    def pz(z: float) -> float:
        return (z_range[1] - z) / max(z_range[1] - z_range[0], 1e-9) * (height - 1)

    segments = _slice_segments(points, triangles, material_ids, y)
    # Draw low materials first so the upper interfaces stay visible.
    for material, rows in sorted(segments.items(), key=lambda kv: min(r[1] for r in kv[1])):
        colour = colors.get(material, "#999999")
        for x0, z0, x1, z1 in rows:
            draw.line([(px(x0), pz(z0)), (px(x1), pz(z1))], fill=colour, width=2)
    del names
    return image


def _default_y(frame: dict[str, Any]) -> float:
    """Slice between two grid lines.

    Slicing exactly on a grid plane lands every intersection on a shared vertex, so
    the segments collapse to points and the picture comes out dotted (measured).
    """
    y_min, y_max = frame["bbox"][1], frame["bbox"][4]
    return y_min + (y_max - y_min) * 0.5037


def _grid(frames: list[Path], out: Path, y: float, cols: int, tile: tuple[int, int]) -> None:
    from PIL import Image, ImageDraw

    tiles = []
    for path in frames:
        frame = json.loads(path.read_text())
        tiles.append((path.stem.split("-")[0], render(frame, y, *tile)))
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile[0], rows * (tile[1] + 14)), "white")
    pen = ImageDraw.Draw(sheet)
    for index, (label, tile_image) in enumerate(tiles):
        col, row = index % cols, index // cols
        sheet.paste(tile_image, (col * tile[0], row * (tile[1] + 14)))
        pen.text((col * tile[0] + 4, row * (tile[1] + 14) + tile[1] + 1), label, fill="black")
    sheet.save(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frame", nargs="?", type=Path)
    parser.add_argument("--grid", type=Path)
    parser.add_argument("--y", type=float, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cols", type=int, default=8)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    args = parser.parse_args(argv)

    if args.grid:
        frames = sorted(args.grid.glob("*.mesh.json"))
        if not frames:
            parser.error(f"no frames under {args.grid}")
        first = json.loads(frames[0].read_text())
        y = args.y if args.y is not None else _default_y(first)
        _grid(frames, args.out, y, args.cols, (args.width // 2, args.height // 2))
        print(f"{len(frames)} frames -> {args.out}")
        return 0

    if not args.frame:
        parser.error("give a frame or --grid")
    frame = json.loads(args.frame.read_text())
    y = args.y if args.y is not None else _default_y(frame)
    render(frame, y, args.width, args.height).save(args.out)
    print(f"{args.frame} y={y:.4f} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
