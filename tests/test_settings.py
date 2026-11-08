"""Persistent global settings: store, validation, precedence, and API shape."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WORKBENCH_PORT", "8767")

from pydantic import ValidationError

from backend import runtime_limits, settings  # noqa: E402
from backend import main as backend_main  # noqa: E402
from backend.models import ProjectV7  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class SettingsTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="test-settings-", dir=ROOT / "var"))
        self.env = patch.dict(
            os.environ,
            {
                "VIENNAPS_WORKBENCH_DATA_DIR": str(self.data_dir),
                "WORKBENCH_PORT": "8767",
            },
            clear=True,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    async def get_settings(self) -> tuple[int, dict]:
        response = await asgi_request("/api/settings")
        return response[0], json_body(response)

    async def put_settings(self, payload: object) -> tuple[int, dict]:
        response = await asgi_request("/api/settings", method="PUT", payload=payload)
        return response[0], json_body(response)


class Defaults(SettingsTestBase):
    async def test_defaults_are_reported_without_a_settings_file(self) -> None:
        status, body = await self.get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(body["schemaVersion"], 1)
        expected = {
            "maxDataGiB": (10, 1, "GiB", "数据目录总预算"),
            "artifactLimitMiB": (4096, 64, "MiB", "单 run artifact 上限"),
            "rssLimitMiB": (8192, 512, "MiB", "worker RSS 软上限"),
            "maxLogMiB": (4, 1, "MiB", "单 run 日志上限"),
            "minFreeGiB": (20, 0, "GiB", "最小空闲磁盘"),
        }
        self.assertEqual(set(body["limits"]), set(expected))
        self.assertEqual(body["defaults"], {"solverTimeoutSeconds": 600})
        for key, (value, minimum, unit, label) in expected.items():
            item = body["limits"][key]
            self.assertEqual(item["value"], value)
            self.assertEqual(item["source"], "default")
            self.assertEqual(item["default"], value)
            self.assertEqual(item["minimum"], minimum)
            self.assertIsNone(item["maximum"])
            self.assertIsNone(item["envValue"])
            self.assertEqual(item["unit"], unit)
            self.assertEqual(item["label"], label)
            self.assertTrue(item["envVariable"].startswith("WORKBENCH_"))
        self.assertFalse(self.settings_file().exists())

    async def test_versions_and_storage_keys_are_present(self) -> None:
        _status, body = await self.get_settings()
        self.assertEqual(set(body["versions"]), {"viennaps", "viennals"})
        self.assertEqual(
            set(body["storage"]),
            {"dataDirectory", "usedBytes", "freeBytes", "totalBytes", "runs",
             "projects", "domainStates", "gdsAssets", "csvAssets"},
        )
        self.assertEqual(body["storage"]["dataDirectory"], str(self.data_dir.resolve()))


class Persistence(SettingsTestBase):
    async def test_put_persists_and_get_round_trips(self) -> None:
        status, body = await self.put_settings({
            "limits": {"maxDataGiB": 20, "artifactLimitMiB": 2048},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["limits"]["maxDataGiB"]["value"], 20)
        self.assertEqual(body["limits"]["maxDataGiB"]["source"], "settings")
        self.assertEqual(body["limits"]["artifactLimitMiB"]["value"], 2048)

        path = self.settings_file()
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw, {"schemaVersion": 1,
                               "limits": {"maxDataGiB": 20, "artifactLimitMiB": 2048},
                               "defaults": {}})
        self.assertFalse(list(self.data_dir.glob(".settings.json.*.tmp")))

        status, fetched = await self.get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(fetched["limits"]["maxDataGiB"]["value"], 20)
        self.assertEqual(fetched["limits"]["artifactLimitMiB"]["value"], 2048)

    async def test_legacy_file_without_defaults_still_loads(self) -> None:
        self.settings_file().write_text(
            json.dumps({"schemaVersion": 1, "limits": {"maxLogMiB": 8}}),
            encoding="utf-8",
        )
        status, body = await self.get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(body["limits"]["maxLogMiB"]["value"], 8)
        self.assertEqual(body["defaults"], {"solverTimeoutSeconds": 600})

    async def test_solver_timeout_default_persists_and_round_trips(self) -> None:
        status, body = await self.put_settings({"defaults": {"solverTimeoutSeconds": 1200}})
        self.assertEqual(status, 200)
        self.assertEqual(body["defaults"], {"solverTimeoutSeconds": 1200})

        raw = json.loads(self.settings_file().read_text(encoding="utf-8"))
        self.assertEqual(raw["defaults"], {"solverTimeoutSeconds": 1200})

        status, fetched = await self.get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(fetched["defaults"], {"solverTimeoutSeconds": 1200})

        # A partial PUT that only touches limits keeps the stored default.
        status, body = await self.put_settings({"limits": {"maxLogMiB": 8}})
        self.assertEqual(status, 200)
        self.assertEqual(body["defaults"], {"solverTimeoutSeconds": 1200})
        status, fetched = await self.get_settings()
        self.assertEqual(status, 200)
        self.assertEqual(fetched["defaults"], {"solverTimeoutSeconds": 1200})

    async def test_null_removes_only_that_override(self) -> None:
        await self.put_settings({"limits": {"maxDataGiB": 25, "minFreeGiB": 30}})
        status, body = await self.put_settings({"limits": {"maxDataGiB": None}})
        self.assertEqual(status, 200)
        self.assertEqual(body["limits"]["maxDataGiB"]["value"], 10)
        self.assertEqual(body["limits"]["maxDataGiB"]["source"], "default")
        self.assertEqual(body["limits"]["minFreeGiB"]["value"], 30)
        stored = json.loads(self.settings_file().read_text(encoding="utf-8"))
        self.assertEqual(stored["limits"], {"minFreeGiB": 30})

    async def test_settings_file_is_atomic_valid_json(self) -> None:
        await self.put_settings({"limits": {"rssLimitMiB": 4096}})
        path = self.settings_file()
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('{\n  "schemaVersion": 1,'), text)
        self.assertIn('\n  "limits": {', text)
        self.assertEqual(json.loads(text)["limits"], {"rssLimitMiB": 4096})
        # The temporary file is renamed into place, never left behind.
        self.assertEqual(sorted(item.name for item in self.data_dir.iterdir()), ["settings.json"])


class Validation(SettingsTestBase):
    async def test_below_minimum_is_rejected(self) -> None:
        status, body = await self.put_settings({"limits": {"maxDataGiB": 0}})
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"][0]["path"], "limits.maxDataGiB")
        self.assertIn(">= 1", body["detail"][0]["message"])
        self.assertFalse(self.settings_file().exists())

    async def test_value_above_environment_cap_is_rejected(self) -> None:
        with patch.dict(os.environ, {"WORKBENCH_MAX_DATA_GIB": "15"}):
            status, body = await self.put_settings({"limits": {"maxDataGiB": 20}})
            self.assertEqual(status, 422)
            self.assertEqual(body["detail"][0]["path"], "limits.maxDataGiB")
            self.assertIn("WORKBENCH_MAX_DATA_GIB", body["detail"][0]["message"])

            status, body = await self.put_settings({"limits": {"maxDataGiB": 12}})
        self.assertEqual(status, 200)
        item = body["limits"]["maxDataGiB"]
        self.assertEqual(item["value"], 12)
        self.assertEqual(item["source"], "settings")
        self.assertEqual(item["maximum"], 15)
        self.assertEqual(item["envValue"], "15")
        self.assertEqual(runtime_limits.max_data_gib(), 12)

    async def test_min_free_below_environment_floor_is_rejected(self) -> None:
        with patch.dict(os.environ, {"WORKBENCH_MIN_FREE_GIB": "25"}):
            status, body = await self.put_settings({"limits": {"minFreeGiB": 10}})
            self.assertEqual(status, 422)
            self.assertEqual(body["detail"][0]["path"], "limits.minFreeGiB")
            self.assertIn("WORKBENCH_MIN_FREE_GIB", body["detail"][0]["message"])

            status, body = await self.put_settings({"limits": {"minFreeGiB": 30}})
        self.assertEqual(status, 200)
        item = body["limits"]["minFreeGiB"]
        self.assertEqual(item["value"], 30)
        self.assertEqual(item["source"], "settings")
        self.assertIsNone(item["maximum"])
        self.assertEqual(item["envValue"], "25")
        self.assertEqual(runtime_limits.min_free_gib(), 30)

    async def test_unknown_limit_and_bad_type_are_rejected(self) -> None:
        status, body = await self.put_settings({"limits": {"notALimit": 1}})
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"][0]["path"], "limits.notALimit")
        status, body = await self.put_settings({"limits": {"maxLogMiB": "4"}})
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"][0]["path"], "limits.maxLogMiB")

    async def test_solver_timeout_default_out_of_range_is_rejected(self) -> None:
        for value in (4, 86401):
            with self.subTest(value=value):
                status, body = await self.put_settings(
                    {"defaults": {"solverTimeoutSeconds": value}})
                self.assertEqual(status, 422)
                self.assertEqual(body["detail"][0]["path"],
                                 "defaults.solverTimeoutSeconds")
                self.assertFalse(self.settings_file().exists())

    async def test_solver_timeout_default_null_is_rejected(self) -> None:
        status, body = await self.put_settings(
            {"defaults": {"solverTimeoutSeconds": None}})
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"][0]["path"], "defaults.solverTimeoutSeconds")
        self.assertFalse(self.settings_file().exists())

    async def test_unknown_defaults_key_is_rejected(self) -> None:
        status, body = await self.put_settings({"defaults": {"notADefault": 1}})
        self.assertEqual(status, 422)
        self.assertEqual(body["detail"][0]["path"], "defaults.notADefault")
        self.assertFalse(self.settings_file().exists())


class SolverTimeoutModel(unittest.TestCase):
    def project(self) -> dict:
        fixture = ROOT / "tests" / "fixtures" / "finfet22_project.json"
        return json.loads(fixture.read_text(encoding="utf-8"))

    def test_project_accepts_solver_timeout_above_the_old_cap(self) -> None:
        project = self.project()
        project["solver"]["timeoutSeconds"] = 1200
        validated = ProjectV7.model_validate(project)
        self.assertEqual(validated.solver.timeoutSeconds, 1200)

        project["solver"]["timeoutSeconds"] = 86401
        with self.assertRaises(ValidationError):
            ProjectV7.model_validate(project)


class DynamicLimits(SettingsTestBase):
    async def test_runtime_limits_reflect_settings_without_restart(self) -> None:
        self.assertEqual(runtime_limits.artifact_limit_mib(), 4096)
        self.assertEqual(runtime_limits.max_data_gib(), 10)
        self.assertEqual(runtime_limits.min_free_gib(), 20)
        self.assertEqual(runtime_limits.rss_limit_mib(), 8192)
        self.assertEqual(runtime_limits.max_log_mib(), 4)

        status, _body = await self.put_settings({"limits": {
            "maxDataGiB": 30,
            "artifactLimitMiB": 8192,
            "rssLimitMiB": 4096,
            "maxLogMiB": 8,
            "minFreeGiB": 5,
        }})
        self.assertEqual(status, 200)
        self.assertEqual(runtime_limits.max_data_gib(), 30)
        self.assertEqual(runtime_limits.max_data_bytes(), 30 * 1024**3)
        self.assertEqual(runtime_limits.artifact_limit_mib(), 8192)
        self.assertEqual(runtime_limits.artifact_limit_bytes(), 8192 * 1024**2)
        self.assertEqual(runtime_limits.rss_limit_mib(), 4096)
        self.assertEqual(runtime_limits.max_log_mib(), 8)
        self.assertEqual(runtime_limits.max_log_bytes(), 8 * 1024**2)
        self.assertEqual(runtime_limits.min_free_gib(), 5)

        status = runtime_limits.deployment_limit_status()
        self.assertEqual(status["maximumDataGiB"], 30)
        self.assertEqual(status["maximumDataSource"], "settings")
        self.assertEqual(status["artifactLimitMiB"], 8192)
        self.assertEqual(status["artifactLimitSource"], "settings")
        self.assertEqual(status["rssLimitMiB"], 4096)
        self.assertEqual(status["maximumLogMiB"], 8)
        self.assertEqual(status["minimumFreeGiB"], 5)

        capabilities = await backend_main.capabilities()
        self.assertEqual(capabilities["runtimeLimits"]["maximumDataGiB"], 30)
        self.assertEqual(capabilities["runtimeLimits"]["artifactLimitMiB"], 8192)
        self.assertEqual(capabilities["resourcePolicy"]["run"]["maximumArtifactBytes"],
                         8192 * 1024**2)

        # Removing the overrides falls back to the compiled defaults immediately.
        await self.put_settings({"limits": {
            "maxDataGiB": None,
            "artifactLimitMiB": None,
            "rssLimitMiB": None,
            "maxLogMiB": None,
            "minFreeGiB": None,
        }})
        self.assertEqual(runtime_limits.max_data_gib(), 10)
        self.assertEqual(runtime_limits.artifact_limit_mib(), 4096)
        self.assertEqual(runtime_limits.rss_limit_mib(), 8192)
        self.assertEqual(runtime_limits.max_log_mib(), 4)
        self.assertEqual(runtime_limits.min_free_gib(), 20)


class StorageUsage(SettingsTestBase):
    async def test_storage_counts_and_bytes(self) -> None:
        (self.data_dir / "runs" / "run-1").mkdir(parents=True)
        (self.data_dir / "runs" / "run-1" / "log.txt").write_bytes(b"x" * 100)
        (self.data_dir / "projects").mkdir()
        (self.data_dir / "projects" / "project.json").write_bytes(b"p" * 10)
        (self.data_dir / "domain-states").mkdir()
        (self.data_dir / "domain-states" / ("a" * 64 + ".vpsd")).write_bytes(b"d" * 20)
        (self.data_dir / "gds-assets").mkdir()
        (self.data_dir / "gds-assets" / ("b" * 64 + ".gds")).write_bytes(b"g" * 30)
        (self.data_dir / "csv-assets").mkdir()
        (self.data_dir / "csv-assets" / ("c" * 64 + ".csv")).write_bytes(b"c" * 40)

        status, body = await self.get_settings()
        self.assertEqual(status, 200)
        storage = body["storage"]
        self.assertEqual(storage["usedBytes"], 200)
        self.assertEqual(storage["runs"], 1)
        self.assertEqual(storage["projects"], 1)
        self.assertEqual(storage["domainStates"], 1)
        self.assertEqual(storage["gdsAssets"], 1)
        self.assertEqual(storage["csvAssets"], 1)
        self.assertGreater(storage["freeBytes"], 0)
        self.assertGreaterEqual(storage["totalBytes"], storage["freeBytes"])


if __name__ == "__main__":
    unittest.main()
