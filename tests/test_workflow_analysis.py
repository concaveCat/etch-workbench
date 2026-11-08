"""P3 snapshot comparison, process window, report and calibration."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import time
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from backend.workflow.analysis import (  # noqa: E402
    _fmt_number,
    _measurement_has_value,
    _method_label,
    _reason_detail,
    _reason_text,
    _sig4,
    _status_label,
    _svg_for_snapshot,
    compare_snapshots,
    experiment_csv,
    process_window,
)
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_adoption import project  # noqa: E402


class AnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        suffix = uuid.uuid4().hex[:6]
        self.metric_id = f"metric_an_{suffix}"
        self.recipe_id = f"recipe_an_{suffix}"
        for path, payload in (
            ("/api/v1/workflow/metrics", {"schemaVersion": 1, "metricId": self.metric_id, "name": "depth", "unit": "um"}),
            ("/api/v1/workflow/metrology-recipes", {
                "schemaVersion": 1, "recipeId": self.recipe_id, "version": 1, "metricId": self.metric_id,
                "name": "depth", "dimension": 2, "method": "etch_depth",
                "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0}, "material": "Si",
            }),
        ):
            await asgi_request(path, method="POST", payload=payload)
        base = project()
        base["steps"][0].update({"rateMagnitude": 0.02, "duration": 0.02})
        adopted = await asgi_request("/api/v1/workflow/flows/from-project", method="POST", payload={"project": base})
        self.assertEqual(adopted[0], 201, adopted[2])
        self.adopted = json_body(adopted)
        self.step_id = self.adopted["compile"]["sourceMap"][-1]["stepId"]
        created = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={
                "name": "analysis three", "project": base, "flowId": self.adopted["flowId"],
                "compileId": self.adopted["compileId"], "recipeId": self.recipe_id, "recipeVersion": 1,
                "variants": [
                    {"parameters": {"step_etch_01": {"duration": duration}}, "replicates": 2, "seed": 700 + index * 10}
                    for index, duration in enumerate((0.02, 0.04, 0.06))
                ],
                "maxAttempts": 2,
            },
        )
        self.assertEqual(created[0], 201, created[2])
        self.experiment = json_body(created)
        started = await asgi_request(
            f"/api/v1/workflow/experiments/{self.experiment['experimentId']}/run", method="POST", payload={}
        )
        self.assertEqual(started[0], 200, started[2])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            fetched = await asgi_request(f"/api/v1/workflow/experiments/{self.experiment['experimentId']}")
            self.experiment = json_body(fetched)
            if self.experiment["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.3)
        self.assertEqual(self.experiment["status"], "completed", self.experiment)
        self.run_ids = [variant["replicates"][0]["runId"] for variant in self.experiment["variants"]]
        self.measurement_id = self.experiment["variants"][0]["replicates"][0]["measurementId"]

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def test_compare_view_process_window_and_report(self):
        candidates = [
            {"runId": run_id, "stepId": self.step_id, "label": f"candidate {index + 1}"}
            for index, run_id in enumerate(self.run_ids)
        ]
        compared = await asgi_request(
            "/api/v1/workflow/analysis/compare", method="POST",
            payload={"candidates": candidates, "tolerance": 1e-4},
        )
        self.assertEqual(compared[0], 200, compared[2])
        comparison = json_body(compared)
        self.assertEqual(len(comparison["candidates"]), 3)
        self.assertEqual(len(comparison["pairwise"]), 3)
        for candidate in comparison["candidates"]:
            self.assertEqual(candidate["summary"]["dimension"], 2)
            self.assertRegex(candidate["summary"]["digest"], r"^[a-f0-9]{64}$")
            self.assertEqual(candidate["summary"]["measureKind"], "length_um")
        self.assertTrue(any(row["status"] in {"different", "within_tolerance"} for row in comparison["pairwise"]))
        self.assertTrue(all(row["materialDelta"] for row in comparison["pairwise"]))

        view = await asgi_request(f"/api/v1/workflow/analysis/candidates/{self.run_ids[0]}/{self.step_id}/view")
        self.assertEqual(view[0], 200, view[2])
        view_body = json_body(view)
        self.assertEqual(view_body["dimension"], 2)
        self.assertTrue(view_body["points"])
        self.assertTrue(view_body["lines"])
        self.assertEqual(len(view_body["lineMaterialIds"]), len(view_body["lines"]))

        window = await asgi_request(
            "/api/v1/workflow/analysis/process-window", method="POST",
            payload={"experimentId": self.experiment["experimentId"], "lower": 0.0001, "upper": 0.0008},
        )
        self.assertEqual(window[0], 200, window[2])
        window_body = json_body(window)
        self.assertEqual(window_body["summary"]["total"], 3)
        self.assertEqual(window_body["summary"]["pass"] + window_body["summary"]["fail"] + window_body["summary"]["uncertain"], 3)
        self.assertTrue(window_body["factorWindows"])
        self.assertTrue(window_body["candidates"])
        self.assertEqual(window_body["candidates"][0]["status"], "pass")
        self.assertTrue(all(row["n"] == 2 for row in window_body["rows"]))

        report = await asgi_request(
            "/api/v1/workflow/analysis/report", method="POST",
            payload={
                "title": "P3 acceptance report", "experimentId": self.experiment["experimentId"],
                "runId": self.run_ids[0], "stepId": self.step_id, "measurementId": self.measurement_id,
                "conclusions": "window candidate selected", "lower": 0.0001, "upper": 0.0008,
            },
        )
        self.assertEqual(report[0], 200, report[2])
        body = json_body(report)
        self.assertRegex(body["reportId"], r"^report_[a-f0-9]{16}$")
        self.assertRegex(body["sha256"], r"^[a-f0-9]{64}$")
        html = body["html"]
        for text in (
            "P3 acceptance report", self.adopted["flowId"], "ViennaPS",
            "depth", "工艺窗口", "window candidate selected",
            "report-svg", "证据", self.measurement_id, "几何类型", "生成时间", "报告摘要",
            "技术详情", "报告哈希", "流程与编译", "几何 / Clip", "引擎与种子", "运行与测量",
            "实验与统计", "结构视图", "结论", "95% 置信区间", "样本数",
            "均值", "标准差", "统计状态", "候选", "成功",
        ):
            self.assertIn(text, html)
        self.assertRegex(html, r"实际种子|种子（请求值）")
        self.assertIn("种子策略", html)
        self.assertIn("共享运行种子", html)
        self.assertNotIn("shared-run-seed", html)
        self.assertNotIn("not-applicable", html)
        self.assertIn("各向同性模型不使用射线通量引擎", html)
        self.assertNotIn("isotropic model does not use a ray flux engine", html)
        # The report timestamp is local wall-clock time, never an ISO UTC string.
        self.assertRegex(html, r"生成时间 \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+")
        self.assertNotRegex(html, r"生成时间 \d{4}-\d{2}-\d{2}T")
        # IDs/hashes live in the technical-details folds, not in the body tables.
        main_body = re.sub(r"<details.*?</details>", "", html, flags=re.S)
        self.assertNotIn(self.measurement_id, main_body)
        self.assertNotIn("depth</td><td>um", main_body)
        self.assertIn("µm", html)
        self.assertNotIn(" um", main_body)
        for stale in ("not recorded", "candidate 1", "No compile envelope selected.",
                      "No run manifest selected.", "No measurement selected.",
                      "Process window", "CI method", "actual seed", "report hash"):
            self.assertNotIn(stale, html)
        self.assertIn("<details", html)
        self.assertIn("打印 / 保存为 PDF", html)
        self.assertEqual(html.count("<h2>"), 7)

        single = await asgi_request(
            "/api/v1/workflow/analysis/report", method="POST",
            payload={"title": "Single run report", "runId": self.run_ids[0], "stepId": self.step_id},
        )
        self.assertEqual(single[0], 200, single[2])
        single_html = json_body(single)["html"]
        for text in (self.adopted["flowId"], "流程与编译", "几何类型", "变量", "编译 ID"):
            self.assertIn(text, single_html)
        self.assertRegex(single_html, r"实际种子|种子（请求值）")
        self.assertNotIn("未选择编译记录", single_html)
        self.assertNotIn("not recorded", single_html)

    def test_process_window_rejects_single_sample_candidates(self):
        document = {
            "experimentId": "exp_single", "metricId": "m", "unit": "um",
            "aggregation": {"rows": [
                {"ordinal": 1, "variantId": "v1", "parameters": {}, "mean": 1.0, "ci95": None, "n": 1, "statisticalStatus": "not_evaluated"},
                {"ordinal": 2, "variantId": "v2", "parameters": {}, "mean": 2.0, "ci95": None, "n": 1, "statisticalStatus": "not_evaluated"},
            ]},
        }
        result = process_window(document, 0.0, 3.0)
        self.assertEqual(result["summary"]["notEvaluated"], 2)
        self.assertEqual(result["summary"]["pass"], 0)
        self.assertEqual(result["candidates"], [])

    def test_structural_diff_checks_connectivity_and_material_units(self):
        points = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]]
        base = {
            "dimension": 3, "topology": "triangles", "points": points,
            "triangles": [[0, 1, 2], [1, 3, 2]], "cellData": {"materialIds": [1, 1]},
            "materials": {"1": {"name": "Mask", "color": "#fff"}, "2": {"name": "Si", "color": "#ccc"}},
        }
        reordered = {**base, "triangles": [[0, 1, 3], [1, 3, 2]]}
        connectivity = compare_snapshots(base, reordered, 1e-3)
        self.assertEqual(connectivity["status"], "different")
        self.assertTrue(any("connectivity" in reason for reason in connectivity["reasons"]))
        material = {**base, "cellData": {"materialIds": [1, 2]}}
        mixed = compare_snapshots(base, material, 1e-3)
        self.assertEqual(mixed["status"], "different")
        self.assertAlmostEqual(mixed["materialTolerance"], 1e-6)
        self.assertTrue(any("material" in reason for reason in mixed["reasons"]))
        line_a = {"dimension": 2, "topology": "lines", "points": [[0, 0, 0], [1, 0, 0], [1, 1, 0]],
                  "lines": [[0, 1]], "cellData": {"materialIds": [1]}, "materials": {"1": {"name": "Si"}}}
        line_b = {**line_a, "lines": [[1, 2]]}
        self.assertEqual(compare_snapshots(line_a, line_b, 1e-6)["status"], "different")

    async def test_calibration_least_squares_and_not_validated(self):
        csv_text = "condition,measured,simulated,weight,split\na,1.0,1.0,1,train\nb,2.0,2.0,1,train\nc,3.1,3.0,1,train\nd,4.0,4.0,1,validation\n"
        dataset = await asgi_request(
            "/api/v1/workflow/calibration/datasets", method="POST",
            payload={"name": "fit one", "csv": csv_text},
        )
        self.assertEqual(dataset[0], 201, dataset[2])
        dataset_body = json_body(dataset)
        self.assertEqual(dataset_body["trainCount"], 3)
        self.assertEqual(dataset_body["validationCount"], 1)
        fitted = await asgi_request(
            "/api/v1/workflow/calibration/fit", method="POST",
            payload={"datasetId": dataset_body["datasetId"], "model": "scale_offset", "method": "least-squares"},
        )
        self.assertEqual(fitted[0], 201, fitted[2])
        fit = json_body(fitted)
        self.assertAlmostEqual(fit["parameters"][0], 1.05, places=8)
        self.assertAlmostEqual(fit["parameters"][1], -0.0666666667, places=7)
        self.assertEqual(fit["trainResiduals"]["n"], 3)
        self.assertEqual(fit["validationResiduals"]["n"], 1)
        self.assertEqual(fit["validationStatus"], "validated")
        fetched = await asgi_request(f"/api/v1/workflow/calibration/fits/{fit['fitId']}")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched)["fitId"], fit["fitId"])

        train_only = await asgi_request(
            "/api/v1/workflow/calibration/datasets", method="POST",
            payload={"name": "train only", "csv": "measured,simulated\n1,1\n2,2\n3,3\n"},
        )
        self.assertEqual(train_only[0], 201, train_only[2])
        not_validated = await asgi_request(
            "/api/v1/workflow/calibration/fit", method="POST",
            payload={"datasetId": json_body(train_only)["datasetId"], "model": "power", "method": "nelder-mead"},
        )
        self.assertEqual(not_validated[0], 201, not_validated[2])
        result = json_body(not_validated)
        self.assertEqual(result["validationStatus"], "not_validated")
        self.assertEqual(result["validationResiduals"]["n"], 0)
        self.assertGreater(result["iterations"], 0)

        invalid = await asgi_request(
            "/api/v1/workflow/calibration/datasets", method="POST",
            payload={"name": "bad", "csv": "measured,other\n1,2\n"},
        )
        self.assertEqual(invalid[0], 422, invalid[2])

        malformed = await asgi_request(
            "/api/v1/workflow/calibration/datasets", method="POST",
            payload={"name": "bad weight", "csv": "measured,simulated,weight\n1,2,not-a-number\n"},
        )
        self.assertEqual(malformed[0], 422, malformed[2])

        zero_power = await asgi_request(
            "/api/v1/workflow/calibration/datasets", method="POST",
            payload={"name": "zero power", "csv": "measured,simulated\n5,0\n2,1\n0.7,2\n0.3,4\n"},
        )
        self.assertEqual(zero_power[0], 201, zero_power[2])
        zero_fit = await asgi_request(
            "/api/v1/workflow/calibration/fit", method="POST",
            payload={"datasetId": json_body(zero_power)["datasetId"], "model": "power", "method": "nelder-mead"},
        )
        self.assertEqual(zero_fit[0], 422, zero_fit[2])
        self.assertIn("positive simulated", json_body(zero_fit)["detail"])
        offset_fit = await asgi_request(
            "/api/v1/workflow/calibration/fit", method="POST",
            payload={"datasetId": json_body(zero_power)["datasetId"], "model": "power_offset", "method": "nelder-mead"},
        )
        self.assertEqual(offset_fit[0], 201, offset_fit[2])
        offset_body = json_body(offset_fit)
        self.assertEqual(len(offset_body["parameters"]), 3)
        self.assertIsNotNone(offset_body["trainResiduals"]["rmse"])


class ReportValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_requires_a_run_measurement_or_experiment(self):
        for payload in ({"title": "Empty report"}, {"title": "Empty report", "conclusions": "none"}):
            response = await asgi_request(
                "/api/v1/workflow/analysis/report", method="POST", payload=payload,
            )
            self.assertEqual(response[0], 422, response[2])
            self.assertIn("报告需要至少一次运行、测量或实验", json.dumps(json_body(response), ensure_ascii=False))


class ReportRenderingTests(unittest.TestCase):
    def test_status_codes_are_localized(self):
        for code, label in (
            ("succeeded", "成功"), ("failed", "失败"), ("running", "运行中"), ("planned", "待执行"),
            ("WITHIN_TOLERANCE", "在公差内"), ("OUTSIDE_TOLERANCE", "超出公差"),
            ("NOT_EVALUATED", "未评估"), ("within_tolerance", "在公差内"),
            ("outside_tolerance", "超出公差"), ("not_evaluated", "未评估"),
        ):
            self.assertEqual(_status_label(code), label)

    def test_2d_svg_uses_height_axis_materials_and_axes(self):
        # h-slot cross section (points are x/h/z with a flat z) must not collapse
        # into a single horizontal line; the outline is a closed material loop.
        snapshot = {
            "dimension": 2, "axisMap": {"x": 0, "h": 1, "z": 2}, "coordinateUnit": "um",
            "topology": "lines",
            "points": [[0, 0, 0], [1, 0, 0], [1, 0.5, 0], [0, 0.5, 0]],
            "lines": [[0, 1], [1, 2], [2, 3], [3, 0]],
            "cellData": {"materialIds": [10, 10, 10, 10]},
            "materials": {"10": {"name": "Si", "color": "#c79a08"}},
        }
        svg = _svg_for_snapshot(snapshot)
        self.assertIn("report-svg", svg)
        self.assertIn("<line", svg)
        self.assertIn("<polygon", svg)
        self.assertIn("材料图例", svg)
        self.assertIn("Si", svg)
        self.assertIn("µm", svg)
        self.assertIn("x (µm)", svg)
        polygon = svg.split("<polygon", 1)[1].split("/>", 1)[0]
        coords = polygon.split('points="', 1)[1].split('"', 1)[0].split()
        self.assertGreater(len({point.split(",")[1] for point in coords}), 1)

        # z-slot cross section (points are x/0/z) follows axisMap.z for the vertical axis.
        z_snapshot = {**snapshot, "points": [[0, 0, 0], [1, 0, 0], [1, 0, 0.5], [0, 0, 0.5]]}
        z_svg = _svg_for_snapshot(z_snapshot)
        self.assertIn("<polygon", z_svg)
        self.assertIn("z (µm)", z_svg)
        z_polygon = z_svg.split("<polygon", 1)[1].split("/>", 1)[0]
        z_coords = z_polygon.split('points="', 1)[1].split('"', 1)[0].split()
        self.assertGreater(len({point.split(",")[1] for point in z_coords}), 1)

    def test_measurement_reasons_are_localized(self):
        self.assertEqual(
            _reason_text("multiple selected-material surfaces cross the measurement x"),
            "测量位置 X 处有多条材料界面相交，无法确定唯一深度；请移动 X 位置或更换测量材料。",
        )
        self.assertEqual(
            _reason_text("no material surface found at the measurement x"),
            "测量位置 X 处没有找到材料界面。",
        )
        self.assertEqual(
            _reason_text("isotropic model does not use a ray flux engine"),
            "各向同性模型不使用射线通量引擎。",
        )
        self.assertIsNone(_reason_detail("multiple selected-material surfaces cross the measurement x"))
        self.assertEqual(_reason_text("some brand new english reason"), "测量无有效值")
        self.assertEqual(_reason_detail("some brand new english reason"), "some brand new english reason")

        class Measurement:
            def __init__(self, status, value):
                self.status, self.value = status, value

        self.assertTrue(_measurement_has_value(Measurement("measured", 0.9)))
        self.assertFalse(_measurement_has_value(Measurement("ambiguous", None)))
        self.assertFalse(_measurement_has_value(Measurement("measured", None)))

    def test_2d_svg_fills_material_to_bottom_and_legend_lists_only_drawn(self):
        snapshot = {
            "dimension": 2, "axisMap": {"x": 0, "h": 1, "z": 2}, "coordinateUnit": "um",
            "topology": "lines",
            "points": [[0, 0, 0], [1, 0, 0], [1, 0.5, 0], [0, 0.5, 0]],
            "lines": [[0, 1], [1, 2], [2, 3], [3, 0]],
            "cellData": {"materialIds": [10, 10, 20, 20]},
            # 30 is registered but never drawn: the legend must not list it.
            "materials": {
                "10": {"name": "Si", "color": "#c79a08"},
                "20": {"name": "Mask", "color": "#4e79a7"},
                "30": {"name": "SiO2", "color": "#59a14f"},
            },
        }
        svg = _svg_for_snapshot(snapshot)
        self.assertIn("<polygon", svg)
        self.assertIn("Si", svg)
        self.assertIn("Mask", svg)
        self.assertNotIn("SiO2", svg)
        # The contour is closed down to the bottom of the plot (18 + 290 = 308).
        first_polygon = svg.split("<polygon", 1)[1].split("/>", 1)[0]
        coords = first_polygon.split('points="', 1)[1].split('"', 1)[0].split()
        self.assertTrue(any(point.endswith(",308.000") for point in coords), coords)

    def test_numbers_keep_four_significant_digits(self):
        # 0.200006 used to print as 0.200006 in reports/CSV while the UI showed 0.2.
        self.assertEqual(_fmt_number(0.200006), "0.2")
        self.assertEqual(_fmt_number(1.234567), "1.235")
        self.assertEqual(_fmt_number(0), "0")
        self.assertEqual(_fmt_number(12000), "12000")
        self.assertEqual(_sig4(0.200006), "0.2")
        self.assertEqual(_sig4(None), "")

    def test_method_names_are_localized(self):
        self.assertEqual(_method_label("etch_depth"), "刻蚀深度")
        self.assertEqual(_method_label("cd_at_height"), "指定高度 CD")
        self.assertEqual(_method_label("brand_new_method"), "brand_new_method")

    def test_experiment_csv_uses_chinese_status_reason_and_um(self):
        class Measurement:
            def __init__(self, status, value, unit, reason):
                self.status, self.value, self.unit, self.reason = status, value, unit, reason

        class Store:
            def get_measurement(self, measurement_id):
                if measurement_id == "mea_1":
                    return Measurement("measured", 0.200006, "um", None)
                return Measurement("ambiguous", None, None, "no material surface found at the measurement x")

        document = {
            "experimentId": "exp_csv",
            "variants": [{
                "variantId": "var_1", "ordinal": 1,
                "parameters": {"step_01": {"duration": 0.200006}},
                "replicates": [
                    {
                        "replicateId": "rep_1", "seed": 700, "status": "succeeded",
                        "value": 0.200006, "unit": "um", "measurementId": "mea_1",
                        "statisticalSample": True, "attemptCount": 1,
                    },
                    {
                        "replicateId": "rep_2", "seed": 701, "status": "succeeded",
                        "value": None, "unit": None, "measurementId": "mea_2",
                        "statisticalSample": True, "attemptCount": 1,
                    },
                    {
                        "replicateId": "rep_3", "seed": 702, "status": "planned",
                        "value": None, "unit": None,
                        "statisticalSample": False, "attemptCount": 0,
                    },
                ],
            }],
            "aggregation": {"unit": "um", "rows": [{
                "variantId": "var_1", "ordinal": 1, "n": 1,
                "mean": 0.200006, "std": None, "ci95": None, "min": 0.200006, "max": 0.200006,
            }]},
        }
        rows = list(csv.DictReader(io.StringIO(experiment_csv(document, Store()))))
        self.assertEqual(len(rows), 3)
        header = rows[0]
        for column in ("status", "reason", "value", "unit"):
            self.assertIn(column, header)
        measured, ambiguous, planned = rows
        self.assertEqual(measured["status"], "成功")
        self.assertEqual(measured["value"], "0.2")
        self.assertEqual(measured["unit"], "µm")
        self.assertEqual(measured["reason"], "")
        self.assertEqual(measured["mean"], "0.2")
        self.assertEqual(ambiguous["status"], "成功")
        self.assertEqual(ambiguous["value"], "")
        self.assertEqual(ambiguous["unit"], "")
        self.assertEqual(ambiguous["reason"], "测量位置 X 处没有找到材料界面。")
        self.assertEqual(planned["status"], "待执行")
        self.assertEqual(planned["reason"], "")
        self.assertNotIn("测量无有效值", planned["reason"])
        # Runner messages are localized; unknown text keeps its own words.
        from backend.workflow.analysis import _replicate_reason_text
        self.assertEqual(_replicate_reason_text("segment finished with failed"), "Segment 运行结束：失败")
        self.assertEqual(_replicate_reason_text("interrupted by server restart"), "服务重启导致中断")
        self.assertEqual(_replicate_reason_text("brand new runner message"), "brand new runner message")

    def test_3d_svg_is_an_isometric_projection(self):
        snapshot = {
            "dimension": 3, "axisMap": {"x": 0, "y": 1, "z": 2}, "coordinateUnit": "um",
            "topology": "triangles",
            "points": [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0.6], [1, 1, 0.6]],
            "triangles": [[0, 1, 2], [2, 3, 4]],
            "cellData": {"materialIds": [10, 20]},
            "materials": {
                "10": {"name": "Si", "color": "#c79a08"},
                "20": {"name": "Mask", "color": "#4e79a7"},
            },
        }
        svg = _svg_for_snapshot(snapshot)
        self.assertIn("等轴投影", svg)
        self.assertGreaterEqual(svg.count("<polygon"), 2)
        self.assertIn("材料图例", svg)
        self.assertIn("Si", svg)
        self.assertIn("Mask", svg)
        self.assertIn(" µm</text>", svg)  # scale bar keeps its unit
        # 8 % margin on every side: the projected top face must not touch the frame.
        coords = [
            tuple(float(value) for value in pair.split(","))
            for polygon in svg.split("<polygon")[1:]
            for pair in polygon.split('points="', 1)[1].split('"', 1)[0].split()
        ]
        xs = [x for x, _ in coords]
        ys = [y for _, y in coords]
        self.assertGreater(min(xs), 62 + 480 * 0.05)
        self.assertLess(max(xs), 62 + 480 * 0.95)
        self.assertGreater(min(ys), 18 + 290 * 0.05)
        self.assertLess(max(ys), 18 + 290 * 0.95)


if __name__ == "__main__":
    unittest.main()
