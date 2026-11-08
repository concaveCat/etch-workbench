"""Schema-7 implant / anneal / analysis capability (ViennaPS 4.7.0 only).

The native classes ``IonImplantation``, ``Anneal``, ``NetDoping`` and
``SheetResistance`` exist only in the 4.7.0 wheel, operate on the Domain cell
set (never on the level sets) and have no model database in the public wheel.
These tests pin the additive schema, the pure-Python rejection paths that keep a
bad request away from a native constructor, and a real 2D/3D native run.

Native evidence (``worker/probe_implant_native.py``, quoted in
``docs/capability-implant.md``):

* 4.7.0 / ViennaLS 5.10.0 expose exactly the setters used by the worker;
* an unmodified Domain cell set contains a single substrate row (100 Si cells
  next to 2000 cover cells on a flat 1 um plane), so the worker widens the band
  on a deep Domain copy and the simulated surface stays byte-identical;
* 2D and 3D implant + anneal + NetDoping + SheetResistance run and repeat
  bit-identically;
* ``viennaps.getModelDbRoot()`` is empty and the wheel ships no model database.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

from backend import preflight, storage  # noqa: E402
from backend.hashing import requested_input_hash  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import ImplantStepV7, parse_project  # noqa: E402
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_engine_cellset import HISTORICAL_HASH, project as cellset_project  # noqa: E402
import backend.snapshots as snapshot_store  # noqa: E402


def solver() -> dict:
    return {
        "timeStepRatio": 0.25, "raysPerPoint": 16, "cpuThreads": 1,
        "random": {"mode": "fixed", "seed": 1234}, "enginePreference": "cpu",
        "timeoutSeconds": 120,
        "advection": {"spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
                      "temporalScheme": "FORWARD_EULER",
                      "adaptiveTimeStepping": False, "adaptiveTimeStepSubdivisions": 20,
                      "calculateIntermediateVelocities": False, "checkDissipation": True,
                      "dissipationAlpha": 1.0, "ignoreVoids": False, "velocityOutput": False},
        "rayTracing": {"diskRadius": 0.0, "ignoreFluxBoundaries": False,
                       "maxBoundaryHits": 80, "maxReflections": 3,
                       "minNodeDistanceFactor": 0.05,
                       "normalizationType": "SOURCE", "smoothingNeighbors": 1},
    }


def cell_set(**overrides) -> dict:
    value = {"height": 0.5, "substrateDepth": 0.4, "coverMaterial": "GAS",
             "periodic": False, "format": "both"}
    value.update(overrides)
    return value


def implant_step(**overrides) -> dict:
    value = {
        "id": "implant_step_01", "name": "P implant", "enabled": True, "model": "implant",
        "species": "P", "material": "Si", "energyKeV": 10.0, "doseCm2": 1.0e15,
        "tiltDeg": 7.0, "doseControl": "WaferDose",
        "profile": {"source": "manual", "kind": "pearsonIV",
                    "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
                    "lateralMu": 0.0, "lateralSigma": 0.005},
        "maskMaterials": [], "screenMaterials": ["SiO2"],
        "damage": {"kind": "hobler", "projectedRange": 0.027, "verticalSigma": 0.018,
                   "lambdaValue": 0.024, "defectsPerIon": 142.9, "lateralSigma": 0.013,
                   "lateralDeltaSigma": -0.02},
        "cellSet": cell_set(),
    }
    value.update(overrides)
    return value


def anneal_step(**overrides) -> dict:
    value = {
        "id": "anneal_step_01", "name": "anneal", "enabled": True, "model": "anneal",
        "mode": "GaussSeidel",
        "schedule": {"kind": "isothermal", "temperatureK": 1273.15, "durationSeconds": 30.0},
        "diffusivity": {"kind": "arrhenius", "D0": 1.23e13, "Ea_eV": 3.566},
        "diffusionMaterials": ["Si"], "blockingMaterials": ["SiO2"],
        "speciesLabel": "P_total", "activeLabel": "P_active",
        "solidActivation": {"solubilityC0": 1.521e2, "solubilityEa_eV": 0.7102},
        "cellSet": cell_set(),
    }
    value.update(overrides)
    return value


def analysis_step(**overrides) -> dict:
    value = {
        "id": "analysis_step_01", "name": "analysis", "enabled": True, "model": "analysis",
        "netDoping": {"donorLabels": ["P_active"], "acceptorLabels": [],
                      "outputLabel": "net_doping"},
        "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
        "cellSet": cell_set(),
    }
    value.update(overrides)
    return value


def project(dimension: int = 2, *, steps: list[dict] | None = None, **overrides) -> dict:
    value = {
        "schemaVersion": 7, "name": f"implant {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": steps if steps is not None else [implant_step(), anneal_step(), analysis_step()],
        "solver": solver(),
    }
    if dimension == 2:
        value["domain"] = {"gridDelta": 0.01, "xExtent": 1.0, "xBoundary": "reflective"}
        value["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        value["domain"] = {"gridDelta": 0.02, "xExtent": 1.0, "yExtent": 1.0,
                           "boundaryXY": "reflective"}
        value["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface",
                           "visibleMaterialIds": [], "opacityByMaterial": {},
                           "clip": {"enabled": False, "axis": "x", "position": 0.0,
                                    "keep": "positive"},
                           "section": {"enabled": False, "axis": "y", "position": 0.0}}
    value.update(overrides)
    return value


def normalized(value: dict) -> dict:
    return validate_project(value)[0].model_dump(mode="json", exclude_none=True)


class ImplantSchema(unittest.TestCase):
    def test_labels_resolve_and_are_recorded(self) -> None:
        step = parse_project(project(2)).steps[0]
        self.assertIsInstance(step, ImplantStepV7)
        self.assertEqual(step.labels.concentration, "P_total")
        self.assertEqual(step.labels.damage, "P_damage")
        self.assertEqual(step.labels.lastDamage, "P_damage_last")
        without_damage = implant_step(damage=None)
        parsed = normalized(project(2, steps=[without_damage]))
        self.assertEqual(set(parsed["steps"][0]["labels"]), {"concentration"})

    def test_reserved_native_labels_are_rejected(self) -> None:
        # ``Material`` / ``FillingFraction`` belong to the native DenseCellSet.
        # Writing a dopant field into one of them overwrote the material scalar
        # (measured: 1500 cells got invalid material IDs ~5.6e8).
        cases = [
            ("implant concentration", [implant_step(labels={"concentration": "Material"}),
                                       anneal_step(), analysis_step()]),
            ("implant damage", [implant_step(labels={"concentration": "P_total",
                                                     "damage": "FillingFraction"}),
                                anneal_step(), analysis_step()]),
            ("implant lastDamage", [implant_step(labels={"concentration": "P_total",
                                                         "lastDamage": "Material"}),
                                    anneal_step(), analysis_step()]),
            ("anneal active", [implant_step(), anneal_step(activeLabel="Material"),
                               analysis_step()]),
            ("anneal species", [implant_step(), anneal_step(speciesLabel="FillingFraction"),
                                analysis_step()]),
            ("net doping output", [implant_step(), anneal_step(), analysis_step(
                netDoping={"donorLabels": ["P_active"], "acceptorLabels": [],
                           "outputLabel": "Material"})]),
            ("net doping input", [implant_step(), anneal_step(), analysis_step(
                netDoping={"donorLabels": ["FillingFraction"], "acceptorLabels": []})]),
            ("sheet resistance", [implant_step(), anneal_step(), analysis_step(
                sheetResistance=[{"concentrationLabel": "Material", "carrier": "hole"}])]),
        ]
        for label, steps in cases:
            with self.subTest(label=label), self.assertRaises(ProjectInputError) as context:
                validate_project(project(2, steps=steps))
            self.assertIn("reserved", str(context.exception))
        # A non-reserved custom label is still accepted and recorded.
        accepted = normalized(project(2, steps=[implant_step(
            labels={"concentration": "As_total"})]))
        self.assertEqual(accepted["steps"][0]["labels"]["concentration"], "As_total")

    def test_cover_material_must_be_gas_for_field_steps(self) -> None:
        # A cover equal to (or any material of) the target is skipped by the
        # native generator: Si cover on a Si substrate returned P_total max 0
        # in 2D and 3D, while GAS kept the substrate visible.
        for dimension in (2, 3):
            for cover in ("Si", "SiO2"):
                steps = [implant_step(cellSet=cell_set(coverMaterial=cover)),
                         anneal_step(cellSet=cell_set(coverMaterial=cover)),
                         analysis_step(cellSet=cell_set(coverMaterial=cover))]
                with self.subTest(dimension=dimension, cover=cover), \
                        self.assertRaises(ProjectInputError) as context:
                    validate_project(project(dimension, steps=steps))
                self.assertIn("GAS", str(context.exception))

    def test_unknown_keys_and_invalid_values_are_rejected(self) -> None:
        cases = [
            (implant_step(extra=1), "extra"),
            (implant_step(doseCm2=0.0), "dose"),
            (implant_step(tiltDeg=120.0), "tilt"),
            (implant_step(rotationDeg=7.0), "rotation"),
            (implant_step(profile={"source": "manual", "kind": "pearsonIV",
                                   "head": {"mu": 1.0, "sigma": 0.0, "beta": 1.0,
                                            "gamma": 0.0},
                                   "lateralMu": 0.0, "lateralSigma": 0.005}), "sigma"),
            (implant_step(profile={"source": "manual", "kind": "dualPearsonIV",
                                   "head": {"mu": 1.0, "sigma": 1.0, "beta": 1.0,
                                            "gamma": 0.0}, "lateralSigma": 0.005}), "tail"),
            (implant_step(damage=None,
                          labels={"concentration": "P_total", "damage": "P_damage"}), "damage"),
            (anneal_step(schedule={"kind": "schedule", "durations": [1.0, 2.0],
                                   "temperatures": [300.0]}), "N+1"),
            (anneal_step(mode="Explicit", implicitSolver={"maxIterations": 10}), "GaussSeidel"),
            (analysis_step(scanDepth=0.1, netDoping=None), "scanDepth"),
            (analysis_step(netDoping=None, sheetResistance=[]), "netDoping"),
        ]
        for step, fragment in cases:
            with self.subTest(fragment=fragment):
                steps = [implant_step(), anneal_step(), analysis_step()]
                steps = [step if item["model"] == step["model"] else item for item in steps]
                with self.assertRaises(ProjectInputError) as context:
                    validate_project(project(2, steps=steps))
                self.assertIn(fragment.lower(), str(context.exception).lower())

    def test_non_finite_numbers_are_rejected(self) -> None:
        for mutation in ({"doseCm2": float("inf")}, {"energyKeV": float("nan")}):
            steps = [implant_step(**mutation), anneal_step(), analysis_step()]
            with self.subTest(mutation=mutation), self.assertRaises(ProjectInputError):
                validate_project(project(2, steps=steps))

    def test_table_model_source_is_refused_with_reason(self) -> None:
        steps = [implant_step(profile={"source": "table", "assetId": "0" * 64}),
                 anneal_step(), analysis_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=steps))
        message = str(context.exception)
        self.assertIn("model database", message)
        self.assertIn("manual", message)

    def test_ordering_and_label_provenance(self) -> None:
        analysis_first = [analysis_step(), implant_step(), anneal_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=analysis_first))
        self.assertIn("earlier", str(context.exception))

        # A surface-moving step between two field steps is allowed: the worker
        # regenerates the native cell set and conservatively remaps the fields.
        moved = [implant_step(), {
            "id": "etch_step_0001", "name": "etch", "enabled": True, "operation": "etch",
            "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1,
            "rateMode": "uniform"}, anneal_step()]
        parsed, warnings = validate_project(project(2, steps=moved))
        self.assertEqual([step["model"] for step in
                          parsed.model_dump(mode="json")["steps"]],
                         ["implant", "isotropic", "anneal"])
        self.assertEqual(warnings, [])

        wrong_label = [implant_step(), anneal_step(activeLabel="B_active"),
                       analysis_step(netDoping={"donorLabels": ["B_active"],
                                                "acceptorLabels": []})]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=wrong_label))
        self.assertIn("not produced by an earlier", str(context.exception))

    def test_substrate_depth_and_cell_set_limits(self) -> None:
        missing = [implant_step(cellSet=cell_set(substrateDepth=None)), anneal_step(),
                   analysis_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=missing))
        self.assertIn("substrateDepth", str(context.exception))

        mismatch = [implant_step(), anneal_step(cellSet=cell_set(substrateDepth=0.2)),
                    analysis_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=mismatch))
        self.assertIn("must match", str(context.exception))

        height_mismatch = [implant_step(), anneal_step(cellSet=cell_set(height=0.3)),
                           analysis_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=height_mismatch))
        self.assertIn("height must match", str(context.exception))

        thin = [implant_step(cellSet=cell_set(substrateDepth=0.005)), anneal_step(),
                analysis_step()]
        with self.assertRaises(ProjectInputError):
            validate_project(project(2, steps=thin))

        huge = [implant_step(cellSet=cell_set(height=1000.0, substrateDepth=1000.0)),
                anneal_step(), analysis_step()]
        with self.assertRaises((ProjectInputError, Exception)) as context:
            validate_project(project(3, steps=huge))
        self.assertIn("limit", str(context.exception))

    def test_periodic_cell_set_must_match_the_domain(self) -> None:
        steps = [implant_step(cellSet=cell_set(periodic=True)), anneal_step(), analysis_step()]
        with self.assertRaises(ProjectInputError) as context:
            validate_project(project(2, steps=steps))
        self.assertIn("periodic", str(context.exception))

    def test_generic_cell_set_output_cannot_be_combined(self) -> None:
        value = project(2)
        value["outputSettings"] = {"cellSet": {"enabled": True, "height": 0.5}}
        with self.assertRaises(ProjectInputError) as context:
            validate_project(value)
        self.assertIn("outputSettings.cellSet", str(context.exception))

    def test_v462_runtime_rejects_every_cell_set_step(self) -> None:
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            with self.assertRaises(ProjectInputError) as context:
                validate_project(project(2))
        message = str(context.exception)
        self.assertIn("4.6.2", message)
        self.assertIn("IonImplantation", message)
        self.assertIn("steps[0].model", message)

    def test_v470_runtime_accepts_the_native_steps(self) -> None:
        with patch("backend.validation.engine_version", return_value="4.7.0"):
            parsed, warnings = validate_project(project(2))
        self.assertEqual([step["model"] for step in
                          parsed.model_dump(mode="json")["steps"]],
                         ["implant", "anneal", "analysis"])
        self.assertEqual(warnings, [])

    def test_omitting_the_new_steps_keeps_the_historical_hash(self) -> None:
        # The cell-set schema-7 project predates this capability; its request
        # hash must not move when no implant/anneal/analysis step is present.
        self.assertEqual(requested_input_hash(validate_project(cellset_project(2))[0]),
                         HISTORICAL_HASH)

    def test_adding_a_step_changes_the_hash(self) -> None:
        base = project(2, steps=[implant_step()])
        extra = project(2, steps=[implant_step(), anneal_step()])
        self.assertNotEqual(requested_input_hash(validate_project(base)[0]),
                            requested_input_hash(validate_project(extra)[0]))

    def test_preflight_counts_each_step_cell_set(self) -> None:
        estimate = preflight.estimate_artifacts(normalized(project(2)))
        kinds = {row["kind"] for row in estimate["contributors"]}
        self.assertIn("optionalOutputs", kinds)
        detail = next(row["detail"] for row in estimate["contributors"]
                      if row["kind"] == "optionalOutputs")
        self.assertIn("implant/anneal/analysis", detail)
        self.assertGreater(estimate["cellSetCellsUpperBound"], 0)


class ImplantWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(2400):
            result = json_body(await asgi_request("/api/runs/" + run_id))
            if result["status"] in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
                return result
            await asyncio.sleep(0.05)
        self.fail("native child exceeded acceptance timeout")

    async def run_project(self, value: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        result = await self.wait(run_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        return run_id, storage.read_json(storage.run_path(run_id) / "manifest.json")

    def snapshot(self, run_id: str, step_id: str) -> dict:
        snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(storage.run_path(run_id) / f"{step_id}.json")

    def outputs(self, manifest: dict, step_id: str) -> list[dict]:
        row = next(item for item in manifest["snapshots"] if item["stepId"] == step_id)
        return row.get("outputs") or []

    async def test_two_dimensional_manual_implant_anneal_and_analysis(self) -> None:
        run_id, manifest = await self.run_project(project(2))
        for step_id, model in (("implant_step_01", "viennaps.IonImplantation"),
                               ("anneal_step_01", "viennaps.Anneal"),
                               ("analysis_step_01", "viennaps.NetDoping")):
            rows = self.outputs(manifest, step_id)
            kinds = [row["kind"] for row in rows]
            self.assertEqual(kinds.count("cellSetFields"), 1, rows)
            self.assertEqual(kinds.count("cellSetFieldsSummary"), 1, rows)
            payload = self.snapshot(run_id, step_id)
            record = payload["cellSetProcess"]
            self.assertEqual(record["nativeModel"], model)
            vtu = next(row for row in rows if row["kind"] == "cellSetFields")
            self.assertGreater(vtu["sizeBytes"], 0)
            import xml.etree.ElementTree as ET
            root = ET.fromstring((storage.run_path(run_id) / vtu["fileName"]).read_text(encoding="utf-8"))
            arrays = {node.attrib.get("Name")
                      for node in root.findall("UnstructuredGrid/Piece/CellData/DataArray")}
            self.assertIn("P_total", arrays)
            self.assertIn("P_total", vtu["scalarLabels"])
            if step_id == "implant_step_01":
                self.assertIn("P_damage", vtu["scalarLabels"])
                self.assertIn("P_damage_last", vtu["scalarLabels"])
            else:
                self.assertIn("P_active", vtu["scalarLabels"])

        implant = self.snapshot(run_id, "implant_step_01")["cellSetProcess"]
        self.assertEqual(implant["profile"]["nativeConstructor"],
                         "viennaps.ImplantPearsonIV")
        self.assertEqual(implant["effective"]["lengthUnitCm"], 1e-4)
        self.assertFalse(implant["energyKeVNativeEffect"])
        self.assertGreater(implant["fields"]["P_total"]["max"], 0.0)
        totals = implant["fields"]["P_total"]["byMaterial"]
        self.assertGreater(totals["10"]["max"], 0.0)
        self.assertEqual(totals["3"]["max"], 0.0)  # cover cells stay undoped
        self.assertGreater(implant["cellSet"]["numberOfCells"], 0)
        self.assertGreater(implant["cellSet"]["substrateDepth"], 0.0)

        anneal = self.snapshot(run_id, "anneal_step_01")["cellSetProcess"]
        self.assertEqual(anneal["schedule"]["nativeCall"], "setTemperature + setDuration")
        self.assertEqual(anneal["solver"]["mode"], "GaussSeidel")
        self.assertGreater(anneal["activation"]["activeConcentrationSum"], 0.0)
        self.assertGreater(anneal["activation"]["activeFractionOfTotal"], 0.0)
        self.assertLessEqual(anneal["activation"]["activeFractionOfTotal"], 1.0)

        analysis = self.snapshot(run_id, "analysis_step_01")["cellSetProcess"]
        net = analysis["netDoping"]
        self.assertEqual(net["outputLabel"], "net_doping")
        self.assertGreater(net["outputField"]["max"], 0.0)
        self.assertIn("net_doping", analysis["fieldDigests"])
        sheet = analysis["sheetResistance"][0]
        self.assertEqual(sheet["nativeMethod"],
                         "viennaps.SheetResistance.computeElectron")
        self.assertGreater(sheet["sheetResistanceOhmPerSquare"], 0.0)

        # Exported units: the native implant concentration is volumetric in 2D
        # as well (1e12 cm^-3 conversion); sum(C)*delta^2 therefore has unit
        # 1/um, not 1/um^2.
        summary = storage.read_json(storage.run_path(run_id) / "artifacts" /
                                    "analysis_step_01-cell-set-fields.json")
        self.assertEqual(summary["concentrationUnit"], "1/um^3")
        self.assertEqual(summary["concentrationUnitCm3"], 1e12)
        self.assertEqual(summary["integralUnit"], "1/um")
        self.assertIn("net_doping", summary["fields"])

        # The surface itself must be untouched: the same project without the
        # cell-set steps produces an identical surface payload.
        baseline_id, _baseline = await self.run_project(project(
            2, steps=[{"id": "etch_step_0001", "name": "noop", "enabled": True,
                       "operation": "etch", "model": "isotropic", "rateMagnitude": 0.0,
                       "duration": 0.1, "rateMode": "uniform"}]))
        produced = self.snapshot(run_id, "initial")
        baseline = self.snapshot(baseline_id, "initial")
        for key in ("points", "cellData", "surfaceBounds", "materialTops"):
            self.assertEqual(produced.get(key), baseline.get(key), key)
        produced = self.snapshot(run_id, "analysis_step_01")
        baseline = self.snapshot(baseline_id, "etch_step_0001")
        self.assertEqual(produced["points"], baseline["points"])
        self.assertEqual(produced["cellData"], baseline["cellData"])

    async def test_json_only_output_skips_the_vtu(self) -> None:
        value = project(2)
        for step in value["steps"]:
            step["cellSet"]["format"] = "json"
        run_id, manifest = await self.run_project(value)
        rows = self.outputs(manifest, "implant_step_01")
        self.assertEqual([row["kind"] for row in rows], ["cellSetFieldsSummary"])
        summary = storage.read_json(storage.run_path(run_id) / "artifacts" /
                                    "implant_step_01-cell-set-fields.json")
        self.assertIn("P_damage", summary["fields"])
        self.assertIn("P_damage", summary["fieldDigests"])

    async def test_three_dimensional_implant_uses_the_same_native_path(self) -> None:
        value = project(3)
        for step in value["steps"]:
            step["cellSet"]["height"] = 0.2
            step["cellSet"]["substrateDepth"] = 0.3
        run_id, manifest = await self.run_project(value)
        analysis = self.snapshot(run_id, "analysis_step_01")["cellSetProcess"]
        self.assertEqual(analysis["cellSet"]["periodicBoundary"], [False, False, False])
        self.assertGreater(analysis["cellSet"]["numberOfCells"], 0)
        summary = storage.read_json(storage.run_path(run_id) / "artifacts" /
                                    "analysis_step_01-cell-set-fields.json")
        self.assertEqual(summary["dimension"], 3)
        self.assertEqual(summary["grid"]["nativeElementType"], "hexahedron")
        # 3D concentration is still volumetric; sum*delta^3 is dimensionless and
        # is therefore not labelled with a length unit.
        self.assertEqual(summary["concentrationUnit"], "1/um^3")
        self.assertEqual(summary["concentrationUnitCm3"], 1e12)
        self.assertNotIn("integralUnit", summary)
        self.assertTrue(summary["process"]["analysis"]["fieldDigests"])
        self.assertGreater(len(summary["grid"]["bounds"]), 0)

    async def test_replay_reproduces_every_field_digest(self) -> None:
        run_id, _manifest = await self.run_project(project(2))
        original = storage.read_json(storage.run_path(run_id) / "artifacts" /
                                     "analysis_step_01-cell-set-fields.json")
        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(submit[0], 202, submit[2])
        replay_id = json_body(submit)["runId"]
        result = await self.wait(replay_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        replayed = storage.read_json(storage.run_path(replay_id) / "artifacts" /
                                     "analysis_step_01-cell-set-fields.json")
        self.assertEqual(original["fieldDigests"], replayed["fieldDigests"])
        self.assertEqual(original["process"]["analysis"]["sheetResistance"],
                         replayed["process"]["analysis"]["sheetResistance"])

    async def test_rejected_requests_never_start_a_worker(self) -> None:
        with patch("backend.validation.engine_version", return_value="4.6.2"):
            response = await asgi_request("/api/runs", method="POST",
                                          payload={"kind": "simulate", "project": project(2)})
        self.assertEqual(response[0], 422)
        self.assertIn("4.7.0", response[2].decode("utf-8", errors="replace"))


class ImplantWorkerGuards(unittest.TestCase):
    def test_shared_guard_matches_the_schema(self) -> None:
        from backend.implant_models import numeric_guard_messages
        parsed = normalized(project(2))
        for step in parsed["steps"]:
            self.assertEqual(numeric_guard_messages(step, dimension=2, grid_delta=0.01), [])
        broken = copy.deepcopy(parsed["steps"][0])
        broken["cellSet"]["height"] = 0.001
        self.assertIn("one grid interval",
                      numeric_guard_messages(broken, dimension=2, grid_delta=0.01)[0])
        broken = copy.deepcopy(parsed["steps"][0])
        broken["profile"]["head"]["sigma"] = -1.0
        self.assertTrue(numeric_guard_messages(broken, dimension=2, grid_delta=0.01))

    def test_shared_guard_is_finite_safe(self) -> None:
        from backend.implant_models import numeric_guard_messages
        broken = {
            "model": "implant", "doseCm2": float("nan"), "tiltDeg": 7.0,
            "profile": {"source": "manual", "kind": "pearsonIV",
                        "head": {"mu": 1.0, "sigma": 1.0, "beta": 1.0, "gamma": 0.0},
                        "lateralSigma": 1.0},
            "cellSet": {"height": 0.5, "substrateDepth": 0.4},
        }
        messages = numeric_guard_messages(broken, dimension=2, grid_delta=0.01)
        self.assertTrue(any("doseCm2" in message for message in messages))

    def test_shared_guard_rejects_reserved_labels(self) -> None:
        from backend.implant_models import numeric_guard_messages
        parsed = normalized(project(2))
        cases = [
            (0, lambda step: step["labels"].__setitem__("concentration", "Material")),
            (0, lambda step: step["labels"].__setitem__("damage", "FillingFraction")),
            (0, lambda step: step["labels"].__setitem__("lastDamage", "Material")),
            (1, lambda step: step.__setitem__("activeLabel", "Material")),
            (1, lambda step: step.__setitem__("speciesLabel", "FillingFraction")),
            (2, lambda step: step["netDoping"].__setitem__("outputLabel", "Material")),
            (2, lambda step: step["netDoping"]["donorLabels"].append("FillingFraction")),
            (2, lambda step: step["sheetResistance"][0].__setitem__(
                "concentrationLabel", "Material")),
        ]
        for index, mutate in cases:
            step = copy.deepcopy(parsed["steps"][index])
            mutate(step)
            with self.subTest(index=index):
                messages = numeric_guard_messages(step, dimension=2, grid_delta=0.01)
                self.assertTrue(any("reserved" in message for message in messages), messages)

    def test_shared_guard_requires_gas_cover(self) -> None:
        from backend.implant_models import numeric_guard_messages
        parsed = normalized(project(2))
        for dimension in (2, 3):
            step = copy.deepcopy(parsed["steps"][0])
            step["cellSet"]["coverMaterial"] = "Si"
            with self.subTest(dimension=dimension):
                messages = numeric_guard_messages(step, dimension=dimension, grid_delta=0.01)
                self.assertTrue(any("coverMaterial" in message for message in messages), messages)

    def test_field_integral_units_follow_the_volumetric_concentration(self) -> None:
        from worker.simulate import _field_integral
        # One cell with C = 2 um^-3 and delta = 2 um: sum(C)*delta^2 = 8 um^-1.
        self.assertEqual(_field_integral([2.0], grid_delta=2.0, dimension=2), 8.0)
        # The 3D value sum(C)*delta^3 = 16 is dimensionless.
        self.assertEqual(_field_integral([2.0], grid_delta=2.0, dimension=3), 16.0)


class ImplantDirectWorkerGuards(unittest.TestCase):
    """The exact worker path a hand-edited ``input.json`` reaches.

    These bypass ``validate_project`` entirely (like the recorded defects) and
    prove the worker itself refuses the request instead of reporting
    ``WORKER_COMPLETE`` with a corrupt field or a stale grid.
    """

    def run_worker(self, project: dict, timeout: int = 240) -> tuple[int, str, dict, Path]:
        workdir = Path(tempfile.mkdtemp(prefix="implant-direct-", dir=str(ROOT / "var")))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        config = {
            "kind": "simulate", "runId": "implant-direct", "project": project,
            "runSeed": 271828,
            "expectedVersions": {"viennaps": version("viennaps"),
                                 "viennals": version("viennals")},
        }
        input_path = workdir / "input.json"
        input_path.write_text(json.dumps(config), encoding="utf-8")
        run_dir = workdir / "run"
        run_dir.mkdir()
        completed = subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "worker/simulate.py"),
             str(input_path), str(run_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        result_path = run_dir / "worker-result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        return completed.returncode, completed.stdout + completed.stderr, result, run_dir

    def assert_worker_refused(self, project: dict, *fragments: str) -> None:
        code, output, result, _run_dir = self.run_worker(project)
        self.assertNotEqual(code, 0, output)
        self.assertNotIn("WORKER_COMPLETE", output)
        self.assertEqual(result, {})
        for fragment in fragments:
            self.assertIn(fragment, output)

    def test_direct_worker_rejects_reserved_labels(self) -> None:
        value = normalized(project(2))
        value["steps"][0]["labels"]["concentration"] = "Material"
        self.assert_worker_refused(value, "reserved", "Material")

        value = normalized(project(2))
        value["steps"][1]["activeLabel"] = "FillingFraction"
        self.assert_worker_refused(value, "reserved", "FillingFraction")

        value = normalized(project(2))
        value["steps"][2]["sheetResistance"][0]["concentrationLabel"] = "Material"
        self.assert_worker_refused(value, "reserved", "concentrationLabel")

    def test_direct_worker_rejects_target_cover_material_in_2d_and_3d(self) -> None:
        for dimension in (2, 3):
            value = normalized(project(dimension))
            for step in value["steps"]:
                step["cellSet"]["coverMaterial"] = "Si"
            with self.subTest(dimension=dimension):
                self.assert_worker_refused(value, "coverMaterial", "GAS")

    def test_direct_worker_rejects_a_later_cell_step_with_a_different_grid(self) -> None:
        value = normalized(project(2))
        value["steps"][1]["cellSet"]["height"] = 0.3
        self.assert_worker_refused(value, "height must match")

        value = normalized(project(2))
        value["steps"][2]["cellSet"]["substrateDepth"] = 0.2
        self.assert_worker_refused(value, "substrateDepth must match")

    def test_direct_worker_accepts_surface_motion_and_remaps_the_fields(self) -> None:
        value = normalized(project(2))
        value["steps"].insert(1, {
            "id": "etch_step_0001", "name": "etch", "enabled": True,
            "operation": "etch", "model": "isotropic", "rateMagnitude": 0.1,
            "duration": 0.1, "rateMode": "uniform",
        })
        code, output, result, run_dir = self.run_worker(value)
        self.assertEqual(code, 0, output)
        self.assertIn("WORKER_COMPLETE", output)
        self.assertEqual([row["stepId"] for row in result["actualSteps"]],
                         ["implant_step_01", "etch_step_0001", "anneal_step_01",
                          "analysis_step_01"])
        payload = json.loads((run_dir / "artifacts" /
                              "analysis_step_01-cell-set-fields.json").read_text())
        setup = payload["cellSetSetup"]
        self.assertEqual(setup["remapCount"], 1)
        record = setup["remaps"][0]
        self.assertEqual(record["sourceStepId"], "etch_step_0001")
        self.assertEqual(record["remapOfStepId"], "implant_step_01")
        self.assertEqual(record["method"], "same-grid-index-copy")
        self.assertEqual(record["oldCellCount"], record["newCellCount"])
        self.assertGreater(record["removedCells"], 0)
        self.assertGreater(record["mass"]["P_total"]["lost"], 0.0)

    def test_direct_worker_accepts_the_shared_grid(self) -> None:
        code, output, result, _run_dir = self.run_worker(normalized(project(2)))
        self.assertEqual(code, 0, output)
        self.assertIn("WORKER_COMPLETE", output)
        self.assertEqual([row["stepId"] for row in result["actualSteps"]],
                         ["implant_step_01", "anneal_step_01", "analysis_step_01"])



if __name__ == "__main__":
    unittest.main()
