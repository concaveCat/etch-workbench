"""Tests for the multi-engine architecture, StepModel SDK, timing & accuracy fixes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines import probe, protocol, registry, scheduler  # noqa: E402
from engines.sdk import StepModelContext, StepModelRegistry  # noqa: E402
from engines.sources import spx_script  # noqa: E402
from engines.voxel.engine import VoxelEngineAdapter  # noqa: E402


class ProbeAndMetricFixTests(unittest.TestCase):
    def test_extra_z_updates_lattice_shape_consistently(self):
        lat = probe.build_lattice(0.2, 0.2, 0.1, 0.5, 500, extra_z=[0.0123, 0.0456, 0.1789])
        self.assertEqual(len(lat.points), lat.shape[0] * lat.shape[1] * lat.shape[2])

    def test_active_and_feature_agreement_eliminate_void_dilution(self):
        # 8 void-air points + 1 substrate point + 1 wrong thin-film point:
        # raw agreement = 9/10 = 0.90, activeAgreement = 1/2 = 0.50, featureAgreement = 0.0!
        points = (
            [[0.05, 0.05, -0.1]]
            + [[0.05, 0.05, 0.05]]
            + [[0.05, 0.05, 0.5 + 0.1 * k] for k in range(8)]
        )
        left = ["Si", "PolySi", *(["void"] * 8)]
        right = ["Si", "W", *(["void"] * 8)]
        m = probe.occupancy_metrics(points, left, right, left_name="cand", right_name="spx")
        self.assertAlmostEqual(m["agreement"], 0.9)
        self.assertEqual(m["activePoints"], 2)
        self.assertAlmostEqual(m["activeAgreement"], 0.5)
        self.assertEqual(m["featurePoints"], 1)
        self.assertAlmostEqual(m["featureAgreement"], 0.0)
        self.assertEqual(m["topConfusions"][0], {"cand": "PolySi", "spx": "W", "count": 1})

    def test_flow_materials_collects_all_non_mask_layers_across_steps(self):
        job = protocol.JobSpec.from_json({
            "case_id": "mats",
            "engine": "any",
            "domain": {"width": 0.2, "depth": 0.2, "height": 0.5, "grid_delta": 0.01},
            "initial": {"kind": "plane", "substrate": "Si", "substrate_thickness": 0.1},
            "steps": [
                {"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                {"step_id": "s1", "kind": "deposit", "mechanism": "fill",
                 "parameters": {"material": "SiO2", "offset": 0.1}},
                {"step_id": "s2", "kind": "mask", "mechanism": "litho",
                 "parameters": {"layer": 11, "thickness": 0.05, "material": "PMMA"}},
                {"step_id": "s3", "kind": "deposit", "mechanism": "isotropic",
                 "duration": 0.05, "rates": {"W": 1.0}},
            ],
        })
        self.assertEqual(protocol.flow_materials(job, include_masks=False), ["Si", "SiO2", "W"])
        self.assertEqual(
            protocol.flow_materials(job, include_masks=True), ["Si", "SiO2", "PMMA", "W"]
        )


class TranslatorFixTests(unittest.TestCase):
    def test_patterned_resets_after_depo_and_never_masks_etched_material(self):
        exported = textwrap.dedent("""
            spx.procedural.substrate(Location={'Step': 'sub'}, Thickness=(0.3, qupy.Unit('um')))
            spx.procedural.depo_fill(Location={'Step': 'ox'}, MaterialFilter=[[('Material', 'equals', 'Oxide')]], Offset=(0.1, qupy.Unit('um')))
            spx.procedural.litho_patterning(Location={'Step': 'mask'}, LayerNames=['CS'])
            spx.procedural.etch_anisotropic(Location={'Step': 'cut'}, MaterialDependent=[([[('Material', 'equals', 'Oxide')]], (1.0, qupy.Unit('um min^-1')))], Time=(0.1, qupy.Unit('min')))
            spx.procedural.etch_strip(Location={'Step': 'strip'}, MaterialFilter=[[('Material', 'equals', 'Photoresist')]])
            spx.procedural.depo_isotropic(Location={'Step': 'w_dep'}, MaterialFilter=[[('Material', 'equals', 'Tungsten')]], Rate=(1.0, qupy.Unit('um min^-1')), Time=(0.02, qupy.Unit('min')))
            spx.procedural.etch_anisotropic(Location={'Step': 'w_recess'}, MaterialDependent=[([[('Material', 'equals', 'Tungsten')]], (1.0, qupy.Unit('um min^-1')))], Time=(5.0, qupy.Unit('min')))
        """)
        job = spx_script.translate(
            exported,
            case_id="t",
            window=[0.0, 0.0, 0.2, 0.2],
            grid_delta=0.01,
            layout_path="dummy.oas",
            spx_layers={"CS": 15},
            domain_height=0.5,
        )
        by_name = {s.step_id: s for s in job.steps}
        self.assertTrue(by_name["03_cut"].parameters["patterned"])
        self.assertEqual(by_name["03_cut"].parameters["mask"], ["PMMA"])
        # After depo_isotropic of W, w_recess is a blanket etchback, NOT patterned,
        # and W must never appear as its own mask!
        self.assertFalse(by_name["06_w_recess"].parameters["patterned"])
        self.assertEqual(by_name["06_w_recess"].parameters["mask"], [])


class ExternalDiscoveryAndThreeEngineSchedulerTests(unittest.TestCase):
    def test_external_manifest_discovery_and_three_engine_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext_dir = root / "ext_engines" / "fast_mock"
            ext_dir.mkdir(parents=True)
            runner = ext_dir / "runner.py"
            runner.write_text(
                textwrap.dedent("""
                    import json, pathlib, sys
                    job = json.loads(pathlib.Path(sys.argv[1]).read_text())
                    out = pathlib.Path(sys.argv[2])
                    pts = (job.get("probe") or {}).get("points") or []
                    sec = job.get("section")
                    rows = []
                    for s in job["steps"]:
                        rows.append({
                            "step_id": s["step_id"],
                            "status": "ok",
                            "wall_ms": 1.5,
                            "solve_ms": 1.5,
                            "frame_ms": 0.0,
                            "probe_ms": 0.2,
                            "section_ms": 0.1,
                            "frame": None,
                            "probe": ["Si" if p[2] < 0 else ("SiO2" if p[2] < 0.05 else "void") for p in pts],
                            "section": {m: [0.0 if m == "Si" else 0.05 for _ in sec["columns"]] for m in sec["materials"]} if sec else None,
                        })
                    (out / "result.json").write_text(json.dumps({
                        "engine": {"engine_id": "fast_mock", "version": "1.0", "solve_mode": "stepwise", "probe_method": "analytic"},
                        "outcomes": rows,
                        "run": {"solveMode": "stepwise", "solveMs": 3.0},
                    }))
                """),
                encoding="utf-8",
            )
            (ext_dir / "manifest.json").write_text(
                json.dumps({
                    "engine_id": "fast_mock",
                    "label": "External Fast Mock",
                    "role": "candidate",
                    "runtime": "system_python",
                    "entrypoint": "runner.py",
                    "mechanisms": ["initial", "isotropic"],
                    "frame_kinds": ["mesh"],
                    "probe_method": "analytic",
                }),
                encoding="utf-8",
            )

            # Also create a reference mock to test 3-way comparison (voxel + fast_mock vs ref_mock)
            ref_dir = root / "ext_engines" / "ref_mock"
            ref_dir.mkdir(parents=True)
            ref_runner = ref_dir / "runner.py"
            ref_runner.write_text(
                runner.read_text(encoding="utf-8")
                .replace('"fast_mock"', '"ref_mock"')
                .replace('"solveMs": 3.0', '"solveMs": 30.0'),
                encoding="utf-8",
            )
            (ref_dir / "manifest.json").write_text(
                json.dumps({
                    "engine_id": "ref_mock",
                    "label": "Reference Mock",
                    "role": "reference",
                    "runtime": "system_python",
                    "entrypoint": "runner.py",
                    "mechanisms": ["initial", "isotropic"],
                    "frame_kinds": ["raster"],
                    "solve_mode": "batch_route",
                    "probe_method": "meas_probe",
                }),
                encoding="utf-8",
            )

            job = protocol.JobSpec.from_json({
                "case_id": "three-way",
                "engine": "any",
                "domain": {"width": 0.1, "depth": 0.1, "height": 0.2, "grid_delta": 0.01},
                "initial": {
                    "kind": "plane",
                    "substrate": "Si",
                    "substrate_thickness": 0.1,
                    "films": [{"material": "SiO2", "thickness": 0.1}],
                },
                "steps": [
                    {"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    {"step_id": "s1", "kind": "etch", "mechanism": "isotropic",
                     "duration": 0.05, "rates": {"SiO2": -1.0}},
                ],
            })

            with mock.patch.dict(os.environ, {"VIENNAPS_ENGINE_DIRS": str(root / "ext_engines")}):
                regs = registry.registrations()
                self.assertIn("fast_mock", regs)
                self.assertIn("ref_mock", regs)
                self.assertIn("voxel", regs)

                payload = scheduler.compare(
                    job,
                    ["voxel", "fast_mock", "ref_mock"],
                    root / "out3",
                    max_probe_points=400,
                    parallel=False,
                    emit_frames=False,
                    emit_checkpoints=False,
                )

            self.assertEqual(payload["referenceEngine"], "ref_mock")
            self.assertEqual(payload["executionMode"], "sequential")
            self.assertEqual(payload["summary"]["engineCount"], 3)
            self.assertEqual(payload["summary"]["fullyAlignedSteps"], 2)
            # Speedup vs ref_mock (30 ms / 3 ms = 10.0x)
            self.assertAlmostEqual(payload["summary"]["speedupVsReference"]["fast_mock"], 10.0, places=2)
            # Both candidates have pairwise comparisons against ref_mock!
            s1_comps = payload["steps"][1]["comparisons"]
            self.assertIn("voxel|ref_mock", s1_comps)
            self.assertIn("fast_mock|ref_mock", s1_comps)
            self.assertEqual(s1_comps["fast_mock|ref_mock"]["probe"]["agreement"], 1.0)
            self.assertGreaterEqual(s1_comps["voxel|ref_mock"]["probe"]["agreement"], 0.98)


class VoxelDamasceneAndCustomStepModelTests(unittest.TestCase):
    def test_damascene_trench_fill_cmp_and_custom_dishing_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Build a minimal GDS with one trench polygon [0.06..0.14, 0.02..0.18] on layer 15
            import gdstk

            lib = gdstk.Library(unit=1e-6, precision=1e-9)
            cell = lib.new_cell("TOP")
            cell.add(gdstk.rectangle((0.06, 0.02), (0.14, 0.18), layer=15))
            gds_path = root / "damascene.gds"
            lib.write_gds(str(gds_path))

            job = protocol.JobSpec.from_json({
                "case_id": "damascene-dishing",
                "engine": "voxel",
                "domain": {"width": 0.2, "depth": 0.2, "height": 0.4, "grid_delta": 0.005},
                "initial": {
                    "kind": "plane",
                    "substrate": "Si",
                    "substrate_thickness": 0.05,
                    "films": [{"material": "SiO2", "thickness": 0.10}],
                },
                "layout": {"path": str(gds_path)},
                "probe": {
                    "points": [
                        # Field point at z=0.07 -> must stay SiO2!
                        [0.02, 0.10, 0.07],
                        # Trench interior at z=0.06 -> must become Cu!
                        [0.10, 0.10, 0.06],
                        # Trench top dished zone at z=0.097 -> dished to void by cmp_dishing!
                        [0.10, 0.10, 0.097],
                    ]
                },
                "emit_frames": False,
                "emit_checkpoints": False,
                "steps": [
                    {"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    {"step_id": "s1_litho", "kind": "mask", "mechanism": "litho",
                     "parameters": {"layer": 15, "thickness": 0.05, "coat": 0.05,
                                    "polarity": "negative", "material": "PMMA"}},
                    {"step_id": "s2_etch", "kind": "etch", "mechanism": "anisotropic",
                     "duration": 0.06, "rates": {"SiO2": -1.0}},
                    {"step_id": "s3_strip", "kind": "strip", "mechanism": "strip",
                     "parameters": {"materials": ["PMMA"]}},
                    {"step_id": "s4_cu", "kind": "deposit", "mechanism": "isotropic",
                     "duration": 0.08, "rates": {"Cu": 1.0}},
                    {"step_id": "s5_cmp", "kind": "planarize", "mechanism": "cmp",
                     "model": "cmp_dishing",
                     "parameters": {"stop_material": "SiO2", "offset": 0.0,
                                    "metal_material": "Cu", "max_dishing_um": 0.008}},
                ],
            })
            res = VoxelEngineAdapter().run_job(job, root / "out")
            final_probe = res["outcomes"][-1]["probe"]
            self.assertEqual(final_probe, ["SiO2", "Cu", "void"])
            self.assertEqual(res["outcomes"][-1]["metrics"]["model"], "cmp_dishing")

    def test_external_step_model_plugin_file_can_be_loaded_without_editing_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = root / "my_custom_recess.py"
            plugin.write_text(
                textwrap.dedent("""
                    def register_models(registry):
                        @registry.register_model("recess_top_10nm")
                        def recess(state, step, ctx):
                            ktop = state.active_k_top()
                            dk = int(round(0.010 / state.dz))
                            state.grid[:, :, max(0, ktop - dk):ktop] = 0
                            return {"recessedCells": dk}
                """),
                encoding="utf-8",
            )
            job = protocol.JobSpec.from_json({
                "case_id": "plugin-step",
                "engine": "voxel",
                "domain": {"width": 0.1, "depth": 0.1, "height": 0.2, "grid_delta": 0.01},
                "initial": {
                    "kind": "plane",
                    "substrate": "Si",
                    "substrate_thickness": 0.05,
                    "films": [{"material": "W", "thickness": 0.05}],
                },
                "probe": {"points": [[0.05, 0.05, 0.035], [0.05, 0.05, 0.045]]},
                "emit_frames": False,
                "emit_checkpoints": False,
                "steps": [
                    {"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    {"step_id": "s1", "kind": "etch", "mechanism": "custom",
                     "model": "recess_top_10nm"},
                ],
            })
            with mock.patch.dict(os.environ, {"VIENNAPS_STEP_MODEL_MODULES": str(plugin)}):
                res = VoxelEngineAdapter().run_job(job, root / "out")
            self.assertEqual(res["outcomes"][1]["status"], "ok")
            # 0.05 um W recessed by 10 nm -> top is 0.040 um: z=0.035 is W, z=0.045 is void!
            self.assertEqual(res["outcomes"][1]["probe"], ["W", "void"])


class ViennapsMultiLayerRayProbeTests(unittest.TestCase):
    def test_repeated_material_layers_are_resolved_without_collapsing(self):
        # Old height_column probe kept only ONE max(z) per material, so Si -> SiO2(0.05) ->
        # Si3N4(0.08) -> SiO2(0.12) mislabelled z=0.03 as Si3N4 instead of SiO2!
        from engines.viennaps.engine import ViennapsEngineAdapter

        with tempfile.TemporaryDirectory() as tmp:
            job = protocol.JobSpec.from_json({
                "case_id": "vp-multilayer",
                "engine": "viennaps",
                "domain": {"width": 0.1, "depth": 0.1, "height": 0.2, "grid_delta": 0.01},
                "initial": {
                    "kind": "plane",
                    "substrate": "Si",
                    "substrate_thickness": 0.05,
                    "films": [
                        {"material": "SiO2", "thickness": 0.05},
                        {"material": "Si3N4", "thickness": 0.03},
                    ],
                },
                "probe": {
                    "points": [
                        [0.05, 0.05, -0.02],  # Si
                        [0.05, 0.05, 0.025],  # lower SiO2 (NOT Si3N4!)
                        [0.05, 0.05, 0.065],  # middle Si3N4
                        [0.05, 0.05, 0.100],  # upper SiO2
                        [0.05, 0.05, 0.150],  # void
                    ]
                },
                "emit_frames": False,
                "emit_checkpoints": True,
                "steps": [
                    {"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    {"step_id": "s1_top_ox", "kind": "deposit", "mechanism": "fill",
                     "parameters": {"material": "SiO2", "offset": 0.04}},
                ],
            })
            res = ViennapsEngineAdapter().run_job(job, Path(tmp))
            self.assertEqual(
                res["outcomes"][1]["probe"],
                ["Si", "SiO2", "Si3N4", "SiO2", "void"],
            )


class SpxColumnProfileHelperTests(unittest.TestCase):
    def test_intervals_from_thickness_preserves_cavities_and_repeated_materials(self):
        from engines.spx.bridge import (
            intervals_from_thickness_output,
            label_at_z,
            top_of_material_in_intervals,
        )

        # Matches measured SPX output on an overhang cavity stack:
        # top Oxide (0.04) -> Gas cavity (0.03) -> bottom Oxide (0.05) -> Silicon (0.10)
        raw_output = [
            ("F|M0|s3", "Oxide", 0.04),
            ("Gas", "Gas", 0.03),
            ("F|M0|s1", "Oxide", 0.05),
            ("F|M0|s0", "Silicon", 0.10),
        ]
        intervals = intervals_from_thickness_output(raw_output, z_bottom_um=-0.10)
        self.assertEqual(label_at_z(intervals, -0.05), "Si")
        self.assertEqual(label_at_z(intervals, 0.025), "SiO2")
        self.assertEqual(label_at_z(intervals, 0.065), "void")  # buried cavity!
        self.assertEqual(label_at_z(intervals, 0.100), "SiO2")
        self.assertEqual(label_at_z(intervals, 0.150), "void")
        self.assertAlmostEqual(
            top_of_material_in_intervals(intervals, "SiO2", -0.1, 0.2) or 0.0, 0.12, places=6
        )


if __name__ == "__main__":
    unittest.main()
