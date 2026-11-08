"""Stage 7 schema, no-op, replay-engine policy and rate-table contracts."""
from __future__ import annotations

import copy
import unittest

from backend.hashing import requested_input_hash
from backend.models import parse_project
from backend.rates import has_motion, uses_flux
from backend.validation import ProjectInputError, capability_verified, gpu_project_signature, select_engine, validate_project


def multi_project(dimension: int = 2, operation: str = "etch") -> dict:
    project = {
        "schemaVersion": 7, "name": f"MultiParticle {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 2.4,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 2.4, "boundaryXY": "reflective"})},
        "geometry": {"kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
                     "layers": [{"id": "oxide", "name": "oxide", "material": "SiO2", "thickness": 0.4, "role": "film"}],
                     "pattern": {"kind": "none"}},
        "steps": [{"id": "multi_step_01", "name": "multi", "enabled": True, "operation": operation,
                   "model": "multiParticle", "duration": 0.1,
                   **({"depositMaterial": "SiO2"} if operation == "deposit" else {}),
                   "species": [
                       {"kind": "ion", "label": "ion", "sourcePower": 1.0, "thetaRMin": 0.0, "thetaRMax": 50.0,
                        "minAngle": 60.0, "B_sp": -1.0, "meanEnergy": 120.0, "sigmaEnergy": 15.0,
                        "thresholdEnergy": 10.0, "inflectAngle": 25.0, "n": 1},
                       {"kind": "neutral", "label": "neutral", "defaultStickingProbability": 0.4,
                        "stickingProbabilityByMaterial": {"SiO2": 0.6}},
                   ],
                   "rateCoefficients": {"defaultBySpecies": {"ion": 0.02, "neutral": 0.03},
                                        "byMaterial": {"SiO2": {"ion": 0.01, "neutral": 0.05}}}}],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 64, "cpuThreads": 1,
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
        project["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        project["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface",
                             "visibleMaterialIds": [0, 10, 30], "opacityByMaterial": {"0": 1.0, "10": 1.0, "30": 1.0},
                             "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                             "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return project


class Stage7Contract(unittest.TestCase):
    def test_native_species_and_rate_tables_validate_and_affect_request_hash(self):
        project = multi_project()
        parsed, _ = validate_project(project, kind="simulate")
        self.assertEqual(parsed.steps[0].model, "multiParticle")
        first = requested_input_hash(parsed)
        changed = copy.deepcopy(project)
        changed["steps"][0]["rateCoefficients"]["byMaterial"]["SiO2"]["neutral"] = 0.09
        self.assertNotEqual(first, requested_input_hash(validate_project(changed)[0]))
        changed = copy.deepcopy(project)
        changed["steps"][0]["species"][1]["stickingProbabilityByMaterial"]["SiO2"] = 0.8
        self.assertNotEqual(first, requested_input_hash(validate_project(changed)[0]))

    def test_all_zero_rate_relation_is_a_real_noop_without_particle_requirements(self):
        project = multi_project()
        for step in project["steps"]:
            step["rateCoefficients"] = {"defaultBySpecies": {"ion": 0.0, "neutral": 0.0}, "byMaterial": {}}
        parsed, _ = validate_project(project)
        self.assertFalse(has_motion(parsed.steps[0]))
        self.assertFalse(uses_flux(parsed.steps[0]))
        changed_seed = copy.deepcopy(project)
        changed_seed["solver"]["random"]["seed"] += 1
        self.assertEqual(requested_input_hash(parsed), requested_input_hash(validate_project(changed_seed)[0]))

    def test_cpu_rows_remain_scoped_and_gpu_requests_route_without_exact_rows(self):
        for dimension in (2, 3):
            for operation in ("etch", "deposit"):
                self.assertTrue(capability_verified(dimension, "multiParticle", operation, "reflective", "CPU_DISK"))
                project = multi_project(dimension, operation)
                validate_project(project, kind="simulate")
                signature = gpu_project_signature(project, project["steps"][0])
                self.assertTrue(capability_verified(dimension, "multiParticle", operation, "reflective", "GPU_DISK",
                                                    schema_version=7, **signature))
                project["solver"]["enginePreference"] = "gpu"
                parsed, _ = validate_project(project, kind="simulate")
                normalized = parsed.model_dump(mode="json", exclude_none=True)
                self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "periodic", "CPU_DISK"))
        self.assertFalse(capability_verified(2, "multiParticle", "etch", "periodic", "GPU_DISK", schema_version=7))
        periodic = multi_project(2)
        periodic["domain"]["xBoundary"] = "periodic"
        with self.assertRaises(ProjectInputError):
            validate_project(periodic, kind="simulate")
        gpu = multi_project(2)
        gpu["solver"]["enginePreference"] = "gpu"
        validate_project(gpu, kind="simulate")
        gpu["domain"]["xBoundary"] = "periodic"
        parsed, _ = validate_project(gpu, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_DISK")

    def test_schema_seven_rejects_duplicate_particle_labels_and_unknown_materials(self):
        duplicate = multi_project()
        duplicate["steps"][0]["species"][1]["label"] = "ion"
        duplicate["steps"][0]["rateCoefficients"]["defaultBySpecies"] = {"ion": 0.02}
        with self.assertRaises(Exception):
            parse_project(duplicate)
        invalid_material = multi_project()
        invalid_material["steps"][0]["species"][1]["stickingProbabilityByMaterial"] = {"missing": 0.5}
        with self.assertRaises(ProjectInputError):
            validate_project(invalid_material)


if __name__ == "__main__":
    unittest.main()
