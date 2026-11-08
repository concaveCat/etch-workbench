"""Uploaded CSV rate tables and native ``ps.CSVFileProcess`` (schema 7).

The installed ViennaPS 4.6.2 binding reads a rate table with exactly D numeric
columns per row (2D ``x,rate``; 3D ``x,y,rate``) and auto-detects LINEAR/IDW.
A failed load is only logged by the engine, so the workbench validates the table
before it copies it into a run directory and requires an explicit interpolation
mode. These tests pin the verified native semantics:

* positive rates with a downward direction move the surface outward (deposit),
  negative rates etch — the native model has no separate etch/deposit flag;
* LINEAR requires a strictly increasing x column (2D) or a complete rectangular
  x/y grid (3D), otherwise the native interpolator silently returns bad values;
* custom Python interpolators are unreachable.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import shutil
import unittest
from pathlib import Path
from urllib.parse import quote

from backend import snapshots as snapshot_store
from backend import storage
from backend.csv_assets import RateCsvError, canonicalize_rate_csv, inspect_rate_csv
from backend.hashing import requested_input_hash
from backend.main import app
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = storage.VAR / "test-csv-reference"


def base_project(dimension: int, geometry: dict | None = None) -> dict:
    value = {
        "schemaVersion": 7, "name": f"CSV 速率表 {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.25, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 271828}, "enginePreference": "cpu", "timeoutSeconds": 60,
                   "advection": {"spatialScheme": "ENGQUIST_OSHER_1ST_ORDER", "temporalScheme": "FORWARD_EULER",
                                 "adaptiveTimeStepping": False, "adaptiveTimeStepSubdivisions": 20,
                                 "calculateIntermediateVelocities": False, "checkDissipation": True,
                                 "dissipationAlpha": 1.0, "ignoreVoids": False, "velocityOutput": False},
                   "rayTracing": {"diskRadius": 0.0, "ignoreFluxBoundaries": False, "maxBoundaryHits": 80,
                                  "maxReflections": 3, "minNodeDistanceFactor": 0.05,
                                  "normalizationType": "SOURCE", "smoothingNeighbors": 1}},
        "outputSettings": {"levelSets": False, "levelSetMeshes": False, "volumeMesh": False, "nativeMetadata": False},
    }
    if dimension == 2:
        value["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        value["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface",
                           "visibleMaterialIds": [0, 10, 30], "opacityByMaterial": {"0": 1.0, "10": 1.0, "30": 1.0},
                           "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                           "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return value


CSV_2D = b"# x,rate\n-2,0.2\n-1,0.3\n0,0.4\n1,0.5\n2,0.6\n"
CSV_3D = ("\n".join(f"{x},{y},{0.2 + 0.02 * (x + 2) + 0.01 * (y + 2)}"
                    for y in (-2, 0, 2) for x in (-2, 0, 2)) + "\n").encode("ascii")


def csv_step(asset: dict, dimension: int, *, step_id: str = "step_csv_0001", **overrides) -> dict:
    step = {
        "id": step_id, "name": "CSV 速率表", "enabled": True, "operation": "native", "model": "csvProcess",
        "duration": 1.0, "assetId": asset["assetId"], "fileName": asset["fileName"],
        "direction": [0, -1, 0] if dimension == 2 else [0, 0, -1],
        "offset": [0, 0], "isotropicComponent": 0.0, "directionalComponent": 1.0,
        "interpolationMode": "LINEAR", "calculateVisibility": True,
    }
    step.update(overrides)
    return step


async def upload_csv(dimension: int, body: bytes, name: str = "rates.csv") -> tuple[int, bytes]:
    status, _headers, payload = await asgi_request(
        f"/api/csv-assets?file_name={quote(name)}&dimension={dimension}", method="POST", payload=body,
        headers=[(b"content-type", b"text/csv")])
    return status, payload


class CsvParserTests(unittest.TestCase):
    def test_verified_column_schemas(self):
        summary_2d = inspect_rate_csv(CSV_2D, 2)
        self.assertEqual(summary_2d["columns"], ["x", "rate"])
        self.assertTrue(summary_2d["linearCompatible"])
        summary_3d = inspect_rate_csv(CSV_3D, 3)
        self.assertEqual(summary_3d["columns"], ["x", "y", "rate"])
        self.assertTrue(summary_3d["linearCompatible"])
        self.assertEqual(summary_3d["rowCount"], 9)

    def test_canonicalization_normalizes_bom_comments_tabs_crlf_and_blank_lines(self):
        source = (b"\xef\xbb\xbf# x,rate\r\n"
                  b"\t 0,\t0.5 \r\n"
                  b"# second # comment\r\n"
                  b"\r\n"
                  b"1 , 0.4\r\n")
        canonical, summary = canonicalize_rate_csv(source, 2)
        self.assertEqual(canonical, b"0.0,0.5\n1.0,0.4\n")
        self.assertEqual(summary["canonicalSha256"],
                         __import__("hashlib").sha256(canonical).hexdigest())
        self.assertEqual(summary["sizeBytes"], len(canonical))
        self.assertEqual(summary["sourceSizeBytes"], len(source))
        self.assertEqual(summary["canonicalFormat"], "ascii-lf-comma-decimal-v1")
        repeated, repeated_summary = canonicalize_rate_csv(canonical, 2)
        self.assertEqual(repeated, canonical)
        expected_summary = dict(summary)
        expected_summary["sourceSizeBytes"] = len(canonical)
        self.assertEqual(repeated_summary, expected_summary)

    def test_canonical_numbers_are_native_safe(self):
        canonical, _summary = canonicalize_rate_csv(b"0,1e-7\n1,-0.0\n", 2)
        self.assertEqual(canonical, b"0.0,1.0e-7\n1.0,0.0\n")
        canonical_3d, _summary = canonicalize_rate_csv(
            b"0,0,1e+0\n0,1,2.\n1,0,.5\n1,1,-0.0\n", 3)
        self.assertEqual(canonical_3d, b"0.0,0.0,1.0\n0.0,1.0,2.0\n1.0,0.0,0.5\n1.0,1.0,0.0\n")
        # 1e-307 is the smallest normal double, 1e-308 is subnormal and the
        # native reader rejects it (LINEAR then SIGSEGVs).
        normal, _summary = canonicalize_rate_csv(b"0,1e-307\n1,1e-307\n", 2)
        self.assertEqual(normal, b"0.0,1.0e-307\n1.0,1.0e-307\n")
        # Some native libraries in the same process enable flush-to-zero, so
        # 1e-308 may parse as 0.0; either the underflow or the subnormal guard
        # must reject it before the native rate grid sees it.
        for body in (b"0,1e-308\n1,0.5\n", b"0,-5e-324\n1,0.5\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(RateCsvError, "subnormal|underflows"):
                    canonicalize_rate_csv(body, 2)

    def test_rejects_failed_native_load_shapes(self):
        cases = {
            "wrong columns": (b"0,1,2\n1,1,2\n", 2),
            "non finite": (b"0,nan\n1,0.5\n", 2),
            "non numeric": (b"x,0.5\n1,0.5\n", 2),
            "one row": (b"0,0.5\n", 2),
            "hex float": (b"0,0x1p0\n1,0.5\n", 2),
            "inline hash": (b"0,0.5 # x\n1,0.4\n", 2),
            "unicode whitespace": ("0,\u00a00.5\n1,0.4\n".encode("utf-8"), 2),
            "unicode digit": ("0,\uff10.5\n1,0.4\n".encode("utf-8"), 2),
            "underflow": (b"0,1e-400\n1,0.5\n", 2),
            "subnormal": (b"0,1e-308\n1,0.5\n", 2),
            "smallest subnormal": (b"0,5e-324\n1,0.5\n", 2),
            "negative subnormal": (b"0,-1e-308\n1,0.5\n", 2),
            "overflow": (b"0,1e400\n1,0.5\n", 2),
            "control character": (b"0,0.5\x01\n1,0.4\n", 2),
            "second bom": (b"0,0.5\n\xef\xbb\xbf1,0.4\n", 2),
        }
        for label, (body, dimension) in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(RateCsvError):
                    canonicalize_rate_csv(body, dimension)

    def test_linear_compatibility_is_stricter_than_idw(self):
        self.assertFalse(inspect_rate_csv(b"1,0.5\n0,0.4\n", 2)["linearCompatible"])
        incomplete = b"0,0,0.5\n0,2,0.5\n2,0,0.5\n"
        summary = inspect_rate_csv(incomplete, 3)
        self.assertFalse(summary["linearCompatible"])
        self.assertTrue(summary["idwCompatible"])


class CsvAssetApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def native_probe(self, asset_id: str, dimension: int, mode: str, coordinate: list[float],
                           *, neighbors: int = 4, label: str = "probe") -> dict:
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        output_path = REFERENCE_DIR / f"{label}-native-load.json"
        guard_path = REFERENCE_DIR / f"{label}-native-load.guard.json"
        log_path = REFERENCE_DIR / f"{label}-native-load.log"
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(guard_path), "--log-path", str(log_path),
                   "--timeout-seconds", "30", "--max-rss-mib", "2048", "--max-log-bytes", "1048576", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/csv_native_load_probe.py"),
                   str(storage.csv_asset_path(asset_id)), str(dimension), mode,
                   json.dumps(coordinate), str(output_path), str(neighbors)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log_path.read_text(encoding="utf-8", errors="replace"))
        return json.loads(output_path.read_text(encoding="utf-8"))

    async def test_upload_is_content_addressed_and_capped(self):
        status, body = await upload_csv(2, CSV_2D, "rates.csv")
        self.assertEqual(status, 201, body)
        asset = json.loads(body)
        self.assertEqual(asset["assetId"], storage.file_sha256(storage.csv_asset_path(asset["assetId"])))
        self.assertEqual(asset["assetId"], asset["canonicalSha256"])
        self.assertEqual(asset["dimension"], 2)
        self.assertEqual(asset["columns"], ["x", "rate"])
        self.assertTrue(storage.resolve_csv_asset(asset["assetId"]).is_file())
        self.assertTrue(storage.csv_asset_metadata_path(asset["assetId"]).is_file())

        repeated = json.loads((await upload_csv(2, CSV_2D, "rates.csv"))[1])
        self.assertEqual(repeated["assetId"], asset["assetId"])

        changed = json.loads((await upload_csv(2, b"-2,0.2\n2,0.7\n", "rates.csv"))[1])
        self.assertNotEqual(changed["assetId"], asset["assetId"])

        fetched = json_body(await asgi_request(f"/api/csv-assets/{asset['assetId']}"))
        self.assertEqual(fetched["rowCount"], 5)
        self.assertEqual(fetched["fileName"], "rates.csv")
        self.assertEqual(fetched["canonicalFormat"], "ascii-lf-comma-decimal-v1")

    async def test_upload_stores_canonical_bytes_and_native_loads_them(self):
        source = (b"\xef\xbb\xbf# header\n"
                  b" 0,\t0.5 \n"
                  b"# second # comment\n"
                  b"\n"
                  b"1 , 0.4\n")
        status, body = await upload_csv(2, source, "bom-tabs.csv")
        self.assertEqual(status, 201, body)
        asset = json.loads(body)
        stored = storage.csv_asset_path(asset["assetId"]).read_bytes()
        self.assertEqual(stored, b"0.0,0.5\n1.0,0.4\n")
        self.assertEqual(asset["canonicalSha256"], __import__("hashlib").sha256(stored).hexdigest())
        self.assertNotEqual(asset["sourceSha256"], asset["assetId"])
        self.assertNotIn(b"\xef\xbb\xbf", stored)
        self.assertNotIn(b"\t", stored)
        self.assertNotIn(b"#", stored)

        linear = await self.native_probe(asset["assetId"], 2, "LINEAR", [0.5, 0.0, 0.0],
                                         label="bom-linear")
        self.assertTrue(linear["loaded"])
        self.assertAlmostEqual(linear["value"], 0.45, places=6)

        tiny = json.loads((await upload_csv(2, b"0,1e-7\n1,1e-7\n", "tiny.csv"))[1])
        self.assertEqual(storage.csv_asset_path(tiny["assetId"]).read_bytes(), b"0.0,1.0e-7\n1.0,1.0e-7\n")
        tiny_probe = await self.native_probe(tiny["assetId"], 2, "LINEAR", [0.5, 0.0, 0.0],
                                             label="tiny-linear")
        self.assertTrue(tiny_probe["loaded"])
        self.assertAlmostEqual(tiny_probe["value"], 1e-7, places=12)

        # Smallest normal double: accepted and native-safe. 1e-308 is
        # subnormal and must be rejected before any native load.
        normal = json.loads((await upload_csv(2, b"0,1e-307\n1,1e-307\n", "normal.csv"))[1])
        self.assertEqual(storage.csv_asset_path(normal["assetId"]).read_bytes(),
                         b"0.0,1.0e-307\n1.0,1.0e-307\n")
        normal_probe = await self.native_probe(normal["assetId"], 2, "LINEAR", [0.5, 0.0, 0.0],
                                               label="normal-linear")
        self.assertTrue(normal_probe["loaded"])
        self.assertEqual(normal_probe["value"], 1e-307)

        incomplete = b"0,0,1.0\n0,2,1.0\n2,0,1.0\n"
        idw_asset = json.loads((await upload_csv(3, incomplete, "incomplete.csv"))[1])
        idw = await self.native_probe(idw_asset["assetId"], 3, "IDW", [0.5, 0.5, 0.0],
                                      neighbors=3, label="incomplete-idw")
        self.assertTrue(idw["loaded"])
        self.assertAlmostEqual(idw["value"], 1.0, places=6)

    async def test_upload_rejects_bad_names_schemas_and_oversize(self):
        self.assertEqual((await upload_csv(2, CSV_2D, "../rates.csv"))[0], 422)
        self.assertEqual((await upload_csv(2, CSV_2D, "rates\n.csv"))[0], 422)
        self.assertEqual((await upload_csv(2, CSV_2D, "rates\x7f.csv"))[0], 422)
        self.assertEqual((await upload_csv(2, CSV_3D))[0], 422)
        self.assertEqual((await upload_csv(2, b"0,nan\n1,0.5\n"))[0], 422)
        self.assertEqual((await upload_csv(2, b"0,0.5 # inline\n1,0.4\n"))[0], 422)
        self.assertEqual((await upload_csv(2, b"0,1e-400\n1,0.5\n"))[0], 422)
        self.assertEqual((await upload_csv(2, b"0,1e-308\n1,0.5\n"))[0], 422)
        self.assertEqual((await upload_csv(3, CSV_3D))[0], 201)

        too_large = b"0,1\n" * ((16 * 1024 * 1024) // 4 + 1)
        status, _body = await upload_csv(2, too_large)
        self.assertEqual(status, 413)


class CsvProjectValidationTests(unittest.TestCase):
    def setUp(self):
        storage.initialize()

    def asset(self, dimension: int, body: bytes, name: str = "rates.csv") -> dict:
        import hashlib
        canonical, summary = canonicalize_rate_csv(body, dimension)
        asset_id = hashlib.sha256(canonical).hexdigest()
        destination = storage.csv_asset_path(asset_id)
        if not destination.exists():
            destination.write_bytes(canonical)
        metadata = {"assetId": asset_id, "fileName": name,
                    "sourceSha256": hashlib.sha256(body).hexdigest(), **summary}
        storage.atomic_json(storage.csv_asset_metadata_path(asset_id), metadata)
        return metadata

    def test_valid_step_requires_an_explicit_interpolation_mode(self):
        asset = self.asset(2, CSV_2D)
        project = base_project(2)
        project["steps"] = [csv_step(asset, 2)]
        parsed, warnings = validate_project(project, kind="simulate")
        self.assertEqual(parsed.steps[0].interpolationMode, "LINEAR")
        self.assertFalse(any("interpolation" in warning for warning in warnings))

        missing = copy.deepcopy(project)
        del missing["steps"][0]["interpolationMode"]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(missing, kind="simulate")
        self.assertTrue(any("interpolationMode" in issue.path for issue in ctx.exception.issues))

        custom = copy.deepcopy(project)
        custom["steps"][0]["interpolationMode"] = "CUSTOM"
        with self.assertRaises(ProjectInputError):
            validate_project(custom, kind="simulate")

        extra = copy.deepcopy(project)
        extra["steps"][0]["customInterpolator"] = "lambda coordinate: 1.0"
        with self.assertRaises(ProjectInputError):
            validate_project(extra, kind="simulate")

    def test_direction_offset_and_component_bounds(self):
        asset = self.asset(2, CSV_2D)
        project = base_project(2)
        for label, overrides in {
            "zero direction": {"direction": [0, 0, 0]},
            "out of range direction": {"direction": [0, 2, 0]},
            "2D vertical direction": {"direction": [0, 0, 1]},
            "2D second offset": {"offset": [0, 0.5]},
            "offset budget": {"offset": [1e7, 0]},
            "zero components": {"isotropicComponent": 0.0, "directionalComponent": 0.0},
            "idw without neighbors": {"interpolationMode": "IDW"},
            "linear with neighbors": {"interpolationMode": "LINEAR", "idwNeighbors": 4},
        }.items():
            with self.subTest(label=label):
                project["steps"] = [csv_step(asset, 2, **overrides)]
                with self.assertRaises(ProjectInputError):
                    validate_project(project, kind="simulate")

    def test_asset_dimension_linear_mode_and_idw_bounds_are_enforced(self):
        asset_3d = self.asset(3, CSV_3D, "rates3.csv")
        project = base_project(2)
        project["steps"] = [csv_step(asset_3d, 2)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any("uploaded for 3D" in issue.message for issue in ctx.exception.issues))

        unsorted = self.asset(2, b"1,0.5\n0,0.4\n2,0.3\n", "unsorted.csv")
        project = base_project(2)
        project["steps"] = [csv_step(unsorted, 2)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any(issue.path.endswith("interpolationMode") for issue in ctx.exception.issues))

        incomplete = self.asset(3, b"0,0,0.5\n0,2,0.5\n2,0,0.5\n", "incomplete.csv")
        project = base_project(3)
        project["steps"] = [csv_step(incomplete, 3)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any("complete rectangular" in issue.message for issue in ctx.exception.issues))

        project = base_project(2)
        project["steps"] = [csv_step(unsorted, 2, interpolationMode="IDW", idwNeighbors=4)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any(issue.path.endswith("idwNeighbors") for issue in ctx.exception.issues))

    def test_missing_asset_and_hash_change(self):
        asset_a = self.asset(2, CSV_2D)
        asset_b = self.asset(2, b"-2,0.2\n-1,0.3\n0,0.4\n1,0.5\n2,0.7\n", "rates.csv")
        first = base_project(2)
        first["steps"] = [csv_step(asset_a, 2)]
        second = copy.deepcopy(first)
        second["steps"][0]["assetId"] = asset_b["assetId"]
        first_hash = requested_input_hash(validate_project(first)[0])
        self.assertNotEqual(first_hash, requested_input_hash(validate_project(second)[0]))

        missing = base_project(2)
        missing["steps"] = [csv_step({**asset_a, "assetId": "f" * 64}, 2)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(missing, kind="simulate")
        self.assertTrue(any(issue.path.endswith("assetId") for issue in ctx.exception.issues))

    def test_file_name_rejects_control_characters(self):
        asset = self.asset(2, CSV_2D)
        for name in ("rates\n.csv", "rates\x00.csv", "rates\x7f.csv"):
            with self.subTest(name=repr(name)):
                project = base_project(2)
                project["steps"] = [csv_step(asset, 2, fileName=name)]
                with self.assertRaises(ProjectInputError) as ctx:
                    validate_project(project, kind="simulate")
                self.assertTrue(any("fileName" in issue.path for issue in ctx.exception.issues))

    def test_coverage_warning_accounts_for_offset(self):
        project = base_project(2)
        project["steps"] = [csv_step(self.asset(2, b"-2,0.2\n2,0.4\n", "narrow.csv"), 2, offset=[1, 0])]
        _parsed, warnings = validate_project(project, kind="simulate")
        self.assertTrue(any("lookup range [-1, 3]" in warning for warning in warnings), warnings)

        project["steps"] = [csv_step(self.asset(2, b"-3,0.2\n3,0.4\n", "wide.csv"), 2, offset=[1, 0])]
        _parsed, warnings = validate_project(project, kind="simulate")
        self.assertFalse(any("does not cover the native lookup range" in warning for warning in warnings), warnings)

        project = base_project(3)
        project["steps"] = [csv_step(self.asset(3, CSV_3D, "grid.csv"), 3, offset=[0, 1])]
        _parsed, warnings = validate_project(project, kind="simulate")
        self.assertTrue(any("native lookup range [-1, 3] um" in warning and "offset 1" in warning
                            for warning in warnings), warnings)

    def test_noncanonical_stored_asset_is_rejected(self):
        import hashlib
        raw = b"\xef\xbb\xbf0,0.5\n1,0.4\n"
        asset_id = hashlib.sha256(raw).hexdigest()
        storage.csv_asset_path(asset_id).write_bytes(raw)
        storage.atomic_json(storage.csv_asset_metadata_path(asset_id), {
            "assetId": asset_id, "fileName": "legacy.csv", "dimension": 2,
            "canonicalSha256": asset_id,
        })
        project = base_project(2)
        project["steps"] = [csv_step({"assetId": asset_id, "fileName": "legacy.csv"}, 2)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any("not in the canonical" in issue.message for issue in ctx.exception.issues))

    def test_subnormal_rate_is_rejected_before_native_load(self):
        import hashlib
        from worker.csv_process import build_csv_process

        raw = b"0,1e-308\n1,0.5\n"
        asset_id = hashlib.sha256(raw).hexdigest()
        storage.csv_asset_path(asset_id).write_bytes(raw)
        storage.atomic_json(storage.csv_asset_metadata_path(asset_id), {
            "assetId": asset_id, "fileName": "subnormal.csv", "dimension": 2,
        })
        project = base_project(2)
        project["steps"] = [csv_step({"assetId": asset_id, "fileName": "subnormal.csv"}, 2)]
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project, kind="simulate")
        self.assertTrue(any("subnormal" in issue.message or "underflow" in issue.message
                            for issue in ctx.exception.issues), ctx.exception.issues)

        run_dir = storage.VAR / "test-csv-worker-subnormal"
        run_dir.mkdir(parents=True, exist_ok=True)
        copy = run_dir / f"csv-source-{asset_id}.csv"
        copy.write_bytes(raw)
        with self.assertRaisesRegex(RuntimeError, "subnormal|underflow"):
            build_csv_process(None, project, project["steps"][0],
                              config={"csvAssets": {asset_id: str(copy)}}, run_dir=run_dir,
                              material_handle=lambda name: name, material_ids={})

        normal = self.asset(2, b"0,1e-307\n1,1e-307\n", "normal.csv")
        project["steps"] = [csv_step(normal, 2)]
        parsed, _warnings = validate_project(project, kind="simulate")
        self.assertEqual(parsed.steps[0].assetId, normal["assetId"])

    def test_save_reopen_keeps_the_asset_reference(self):
        asset = self.asset(2, CSV_2D)
        project = base_project(2)
        project["steps"] = [csv_step(asset, 2)]
        normalized = validate_project(project)[0].model_dump(mode="json", exclude_none=True)
        record = storage.create_project(normalized)
        reopened = storage.read_json(storage.project_path(record["id"]))["project"]
        self.assertEqual(reopened["steps"][0]["assetId"], asset["assetId"])
        self.assertEqual(reopened["steps"][0]["fileName"], asset["fileName"])
        parsed, _warnings = validate_project(reopened, kind="simulate")
        self.assertEqual(parsed.steps[0].model, "csvProcess")


def material_top_2d_at(snapshot: dict, material_id: int, x: float) -> float | None:
    points = snapshot["points"]
    lines = snapshot.get("lines") or []
    ids = snapshot["cellData"]["materialIds"]
    best: float | None = None
    for line, material in zip(lines, ids):
        if int(material) != material_id or len(line) < 2:
            continue
        a, b = points[line[0]], points[line[1]]
        low, high = min(a[0], b[0]), max(a[0], b[0])
        if x < low - 1e-12 or x > high + 1e-12:
            continue
        if abs(b[0] - a[0]) < 1e-12:
            h = max(a[1], b[1])
        else:
            weight = (x - a[0]) / (b[0] - a[0])
            h = a[1] + weight * (b[1] - a[1])
        best = h if best is None else max(best, h)
    return best


def material_top_at(snapshot: dict, material_id: int, x: float, y: float) -> float | None:
    points = snapshot["points"]
    triangles = snapshot.get("triangles") or []
    ids = snapshot["cellData"]["materialIds"]
    best: float | None = None
    for triangle, material in zip(triangles, ids):
        if int(material) != material_id:
            continue
        a, b, c = (points[index] for index in triangle[:3])
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-12:
            continue
        u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
        v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
        if min(u, v, 1.0 - u - v) < -1e-8:
            continue
        z = u * a[2] + v * b[2] + (1.0 - u - v) * c[2]
        best = z if best is None else max(best, z)
    return best


class CsvNativeReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(2400):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded acceptance timeout")

    async def execute(self, project: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    async def reference(self, project: dict, asset_id: str, label: str) -> dict:
        input_path = REFERENCE_DIR / f"{label}-input.json"
        output_path = REFERENCE_DIR / f"{label}-direct-reference.json"
        guard_path = REFERENCE_DIR / f"{label}-reference-guard.json"
        log_path = REFERENCE_DIR / f"{label}-reference.log"
        input_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(guard_path), "--log-path", str(log_path),
                   "--timeout-seconds", "90", "--max-rss-mib", "2048", "--max-log-bytes", "2097152", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/csv_direct_reference.py"),
                   str(input_path), str(storage.csv_asset_path(asset_id)), str(output_path)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log_path.read_text(encoding="utf-8", errors="replace"))
        return json.loads(output_path.read_text(encoding="utf-8"))

    async def snapshot(self, run_id: str, step_id: str) -> dict:
        snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{step_id}"))

    async def test_2d_and_3d_runs_match_a_direct_native_reference_and_replay(self):
        for dimension, body in ((2, CSV_2D), (3, CSV_3D)):
            with self.subTest(dimension=dimension):
                asset = json.loads((await upload_csv(dimension, body, f"rates{dimension}.csv"))[1])
                project = base_project(dimension)
                project["steps"] = [csv_step(asset, dimension)]
                run_id, info = await self.execute(project)

                manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
                self.assertEqual(manifest["plannedExecution"],
                                 [{"stepId": "step_csv_0001", "engine": "not-applicable", "seed": None}])
                self.assertEqual(manifest["csvAssetIds"], [asset["assetId"]])
                execution = manifest["actualExecution"][-1]["actualParameters"]["csvProcess"]
                self.assertEqual(execution["nativeClass"], "CSVFileProcess")
                self.assertEqual(execution["assetSha256"], asset["assetId"])
                self.assertEqual(execution["interpolationMode"], "LINEAR")
                self.assertEqual(execution["dimension"], dimension)

                direct = await self.reference(project, asset["assetId"], f"csv-{dimension}d")
                snapshot = await self.snapshot(run_id, "step_csv_0001")
                expected = direct["step_csv_0001"]
                key = "lines" if dimension == 2 else "triangles"
                self.assertEqual(snapshot[key], expected["cells"])
                self.assertEqual(snapshot["cellData"]["materialIds"], expected["materialIds"])
                self.assertEqual(len(snapshot["points"]), len(expected["points"]))
                error = max((math.dist(a, b) for a, b in zip(snapshot["points"], expected["points"])), default=0.0)
                self.assertLessEqual(error, 1e-6, f"{dimension}D maximum coordinate error {error}")
                self.assertGreater(max(point[1 if dimension == 2 else 2] for point in snapshot["points"]), 0.2)

                replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
                replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(replay_response[0], 202, replay_response[2])
                replay_id = json_body(replay_response)["runId"]
                await self.wait(replay_id)
                replayed = await self.snapshot(replay_id, "step_csv_0001")
                for field in ("points", "lines" if dimension == 2 else "triangles", "cellData", "materialIdsByLevelSetIndex"):
                    self.assertEqual(snapshot[field], replayed[field], field)

                # The content-addressed store is the replay input: if the table
                # disappears, replay must fail before a worker starts.
                asset_path = storage.csv_asset_path(asset["assetId"])
                backup = asset_path.with_suffix(".csv.replay-test")
                shutil.move(asset_path, backup)
                try:
                    rejected = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                    self.assertEqual(rejected[0], 422, rejected[2])
                finally:
                    shutil.move(backup, asset_path)

    async def test_2d_idw_mode_matches_a_direct_native_reference(self):
        asset = json.loads((await upload_csv(2, b"1,0.5\n0,0.3\n2,0.2\n", "idw.csv"))[1])
        self.assertFalse(asset["linearCompatible"])
        project = base_project(2)
        project["steps"] = [csv_step(asset, 2, interpolationMode="IDW", idwNeighbors=3)]
        run_id, _info = await self.execute(project)
        direct = await self.reference(project, asset["assetId"], "csv-2d-idw")
        snapshot = await self.snapshot(run_id, "step_csv_0001")
        expected = direct["step_csv_0001"]
        self.assertEqual(snapshot["lines"], expected["cells"])
        error = max((math.dist(a, b) for a, b in zip(snapshot["points"], expected["points"])), default=0.0)
        self.assertLessEqual(error, 1e-6)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["interpolationMode"], "IDW")
        self.assertEqual(execution["idwNeighbors"], 3)

    async def test_2d_process_variants_match_a_direct_native_reference(self):
        cases = [
            ("negative-etch", b"-2,-0.2\n-1,-0.3\n0,-0.4\n1,-0.5\n2,-0.6\n",
             {}, "negative"),
            ("offset", b"-4,0.0\n-3,0.05\n-2,0.1\n-1,0.15\n0,0.2\n1,0.25\n2,0.3\n3,0.35\n4,0.4\n",
             {"offset": [1, 0]}, "offset"),
            ("mixed-iso-invisible", CSV_2D,
             {"isotropicComponent": 0.3, "directionalComponent": 0.7, "calculateVisibility": False}, "mixed"),
        ]
        for label, body, overrides, kind in cases:
            with self.subTest(case=label):
                asset = json.loads((await upload_csv(2, body, f"{label}.csv"))[1])
                project = base_project(2)
                project["steps"] = [csv_step(asset, 2, **overrides)]
                run_id, _info = await self.execute(project)
                direct = await self.reference(project, asset["assetId"], f"csv-2d-{label}")
                snapshot = await self.snapshot(run_id, "step_csv_0001")
                expected = direct["step_csv_0001"]
                self.assertEqual(snapshot["lines"], expected["cells"])
                error = max((math.dist(a, b) for a, b in zip(snapshot["points"], expected["points"])), default=0.0)
                self.assertLessEqual(error, 1e-6)
                heights = [point[1] for point in snapshot["points"]]
                if kind == "negative":
                    self.assertLess(min(heights), -0.1)
                elif kind == "offset":
                    self.assertGreater(max(heights), 0.15)
                else:
                    self.assertGreater(max(heights), 0.5)
                execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
                self.assertEqual(execution["offset"], [float(value) for value in overrides.get("offset", [0, 0])])
                self.assertEqual(execution["calculateVisibility"], overrides.get("calculateVisibility", True))

    async def test_3d_idw_mode_matches_a_direct_native_reference(self):
        incomplete = b"0,0,0.2\n0,2,0.6\n2,0,0.4\n"
        asset = json.loads((await upload_csv(3, incomplete, "idw3d.csv"))[1])
        self.assertFalse(asset["linearCompatible"])
        project = base_project(3)
        project["steps"] = [csv_step(asset, 3, interpolationMode="IDW", idwNeighbors=3)]
        run_id, _info = await self.execute(project)
        direct = await self.reference(project, asset["assetId"], "csv-3d-idw")
        snapshot = await self.snapshot(run_id, "step_csv_0001")
        expected = direct["step_csv_0001"]
        self.assertEqual(snapshot["triangles"], expected["cells"])
        error = max((math.dist(a, b) for a, b in zip(snapshot["points"], expected["points"])), default=0.0)
        self.assertLessEqual(error, 1e-6)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["interpolationMode"], "IDW")
        self.assertEqual(execution["idwNeighbors"], 3)

    async def test_gds_mask_top_layer_stays_immobile(self):
        fixture = ROOT / "docs/gds-mask-diagnosis/fixtures/multi-layer.gds"
        upload = await asgi_request("/api/gds-assets?file_name=multi-layer.gds", method="POST",
                                    payload=fixture.read_bytes(),
                                    headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(upload[0], 201, upload[2])
        gds_asset = json_body(upload)
        geometry = {
            "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
            "layers": [
                {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.5, "role": "film"},
                {"id": "gdsmask", "name": "GDS Mask", "material": "Mask", "thickness": 0.5,
                 "role": "mask", "source": "gds-mask"},
            ],
            "pattern": {"kind": "gds"},
            "gdsMask": {
                "name": "GDS Mask", "assetId": gds_asset["assetId"], "fileName": "multi-layer.gds",
                "structure": "MULTI", "layer": 2, "datatype": 0, "unitScale": 1.0,
                "position": [0.0, 0.0, 0.0], "baseHeight": 0.5, "height": 0.5,
                "material": "Mask", "retainedRegion": "inside", "protectsEtch": True,
            },
        }
        asset = json.loads((await upload_csv(3, CSV_3D, "overlay.csv"))[1])
        project = base_project(3, geometry)
        # Disable visibility here: the installed native prepare() computes one
        # visibility array on the topmost (mask) level set and reuses its point
        # IDs for every other level set. That is not a mask-protection issue,
        # but it can zero the film velocity; the mask immobility assertion is
        # independent of it.
        project["steps"] = [csv_step(asset, 3, calculateVisibility=False)]
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)
        run_id, _info = await self.execute(project)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["maskMaterials"], ["Mask"])
        initial = await self.snapshot(run_id, "initial")
        final = await self.snapshot(run_id, "step_csv_0001")
        # The mask's bottom interface is shared with the growing film and can be
        # relabelled by the surface extractor; the exposed mask top is the
        # physical mask surface and must not move at all.
        for x, y in ((0.0, 0.0), (0.3, 0.5), (-0.3, -0.5)):
            before = material_top_at(initial, 0, x, y)
            after = material_top_at(final, 0, x, y)
            self.assertIsNotNone(before, (x, y))
            self.assertIsNotNone(after, (x, y))
            self.assertGreater(before, 0.5)
            self.assertAlmostEqual(before, after, delta=1e-9, msg=(x, y))

    async def test_legacy_film_only_gds_mask_overlay_stays_immobile(self):
        fixture = ROOT / "docs/gds-mask-diagnosis/fixtures/multi-layer.gds"
        upload = await asgi_request("/api/gds-assets?file_name=multi-layer.gds", method="POST",
                                    payload=fixture.read_bytes(),
                                    headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(upload[0], 201, upload[2])
        gds_asset = json_body(upload)
        # Legacy representation: no role=mask layer and no source=gds-mask
        # layer. The only mask material comes from geometry.gdsMask.protectsEtch.
        geometry = {
            "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
            "layers": [
                {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.5, "role": "film"},
            ],
            "pattern": {"kind": "none"},
            "gdsMask": {
                "name": "GDS Mask", "assetId": gds_asset["assetId"], "fileName": "multi-layer.gds",
                "structure": "MULTI", "layer": 2, "datatype": 0, "unitScale": 1.0,
                "position": [0.0, 0.0, 0.0], "baseHeight": 0.5, "height": 0.5,
                "material": "Mask", "retainedRegion": "inside", "protectsEtch": True,
            },
        }
        asset = json.loads((await upload_csv(3, CSV_3D, "legacy-overlay.csv"))[1])
        project = base_project(3, geometry)
        project["steps"] = [csv_step(asset, 3, calculateVisibility=False)]
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)
        run_id, _info = await self.execute(project)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["maskMaterials"], ["Mask"])
        initial = await self.snapshot(run_id, "initial")
        final = await self.snapshot(run_id, "step_csv_0001")
        for x, y in ((0.0, 0.0), (0.3, 0.5), (-0.3, -0.5)):
            before = material_top_at(initial, 0, x, y)
            after = material_top_at(final, 0, x, y)
            self.assertIsNotNone(before, (x, y))
            self.assertIsNotNone(after, (x, y))
            self.assertGreater(before, 0.5)
            self.assertAlmostEqual(before, after, delta=1e-9, msg=(x, y))

    async def test_2d_masked_fin_preserves_the_fin_mask(self):
        fin = {"kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0.0,
               "maskHeight": 0.5, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
               "maskMaterial": "Mask", "construction": "native"}
        asset = json.loads((await upload_csv(2, b"-2,-0.4\n2,-0.4\n", "fin-etch.csv"))[1])
        project = base_project(2, fin)
        # Visibility is disabled for the same native multi-level-set reason as
        # the GDS overlay test; mask protection is independent of it.
        project["steps"] = [csv_step(asset, 2, calculateVisibility=False)]
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)
        run_id, _info = await self.execute(project)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["maskMaterials"], ["Mask"])
        initial = await self.snapshot(run_id, "initial")
        final = await self.snapshot(run_id, "step_csv_0001")
        mask_before = material_top_2d_at(initial, 0, 0.0)
        mask_after = material_top_2d_at(final, 0, 0.0)
        self.assertIsNotNone(mask_before)
        self.assertIsNotNone(mask_after)
        self.assertGreater(mask_before, 1.0)
        # A zero-velocity level set can still be re-distanced by the native
        # advection pass; the measured coordinate floor is ~2.5e-9 um.
        self.assertAlmostEqual(mask_before, mask_after, delta=5e-9)
        film_before = material_top_2d_at(initial, 10, 1.5)
        film_after = material_top_2d_at(final, 10, 1.5)
        self.assertIsNotNone(film_before)
        self.assertIsNotNone(film_after)
        self.assertLess(film_after, film_before - 0.1)

    async def test_2d_mask_material_is_derived_like_ion_beam(self):
        trench = {"kind": "trench", "width": 1.5, "depth": 0.8, "taperAngle": 0.0,
                  "maskHeight": 0.5, "maskTaperAngle": 0.0, "substrateMaterial": "Si", "maskMaterial": "Mask"}
        asset = json.loads((await upload_csv(2, b"-2,0.2\n2,0.2\n", "mask.csv"))[1])
        project = base_project(2, trench)
        project["steps"] = [csv_step(asset, 2)]
        run_id, _info = await self.execute(project)
        execution = storage.read_json(storage.run_path(run_id) / "manifest.json")["actualExecution"][-1]["actualParameters"]["csvProcess"]
        self.assertEqual(execution["maskMaterials"], ["Mask"])
        self.assertEqual(execution["maskMaterialIds"], [0])
        snapshot = await self.snapshot(run_id, "step_csv_0001")
        self.assertIn(0, set(snapshot["cellData"]["materialIds"]))


if __name__ == "__main__":
    unittest.main()
