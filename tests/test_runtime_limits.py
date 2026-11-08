"""Deployment limits: env parsing, startup wiring, and capabilities exposure."""
from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import main as backend_main
from backend import runtime_limits, storage, supervisor


class LimitParsing(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime_limits.artifact_limit_mib(), 4096)
            self.assertEqual(runtime_limits.artifact_limit_bytes(), 4294967296)
            self.assertEqual(runtime_limits.max_data_gib(), 10)
            self.assertEqual(runtime_limits.min_free_gib(), 20)
            self.assertEqual(runtime_limits.rss_limit_mib(), 8192)
            self.assertEqual(runtime_limits.max_log_mib(), 4)
            self.assertEqual(runtime_limits.max_log_bytes(), 4 * 1024**2)
            self.assertEqual(runtime_limits.artifact_limit_status()["artifactLimitSource"], "default")
            self.assertIsNone(runtime_limits.artifact_limit_status()["artifactLimitMaximumMiB"])

    def test_artifact_cap_has_no_upper_bound(self) -> None:
        for value in (512, 4096, 16384, 65536):
            with self.subTest(value=value), patch.dict(
                    os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": str(value)}):
                self.assertEqual(runtime_limits.artifact_limit_mib(), value)
                self.assertEqual(runtime_limits.artifact_limit_bytes(), value * 1024**2)
                status = runtime_limits.artifact_limit_status()
                self.assertEqual(status["artifactLimitSource"], "env")
                self.assertEqual(status["artifactLimitEnvironmentVariable"], "WORKBENCH_MAX_ARTIFACT_MIB")
                self.assertIsNone(status["artifactLimitMaximumMiB"])

    def test_invalid_artifact_values_fall_back_to_4096(self) -> None:
        for raw in ("", "0", "-5", "63", "abc", "4096.5"):
            with self.subTest(raw=raw), patch.dict(
                    os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": raw}):
                with self.assertLogs("backend.runtime_limits", level="WARNING"):
                    self.assertEqual(runtime_limits.artifact_limit_mib(), 4096)
                self.assertEqual(runtime_limits.artifact_limit_status()["artifactLimitSource"], "default")

    def test_other_limits_and_invalid_fallbacks(self) -> None:
        values = {
            "WORKBENCH_MAX_DATA_GIB": (runtime_limits.max_data_gib, 10, 3),
            "WORKBENCH_MIN_FREE_GIB": (runtime_limits.min_free_gib, 20, 0),
            "WORKBENCH_RSS_LIMIT_MIB": (runtime_limits.rss_limit_mib, 8192, 1024),
            "WORKBENCH_MAX_LOG_MIB": (runtime_limits.max_log_mib, 4, 7),
        }
        for env, (getter, default, valid) in values.items():
            with self.subTest(env=env):
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(getter(), default)
                with patch.dict(os.environ, {env: str(valid)}):
                    self.assertEqual(getter(), valid)
                with patch.dict(os.environ, {env: "-1"}):
                    with self.assertLogs("backend.runtime_limits", level="WARNING"):
                        self.assertEqual(getter(), default)

    def test_large_artifact_cap_does_not_bypass_the_data_budget(self) -> None:
        usage = SimpleNamespace(total=10**13, used=0, free=10**13)
        with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "65536",
                                     "WORKBENCH_MAX_DATA_GIB": "1"}):
            with patch.object(storage.shutil, "disk_usage", return_value=usage), \
                    patch.object(storage, "app_storage_bytes", return_value=2 * 1024**3):
                with self.assertRaisesRegex(OSError, "1 GiB storage budget"):
                    storage.check_disk_preflight()

    def test_free_space_floor_is_configurable(self) -> None:
        usage = SimpleNamespace(total=10**13, used=0, free=1024**3)
        with patch.dict(os.environ, {"WORKBENCH_MIN_FREE_GIB": "5"}):
            with patch.object(storage.shutil, "disk_usage", return_value=usage), \
                    patch.object(storage, "app_storage_bytes", return_value=0):
                with self.assertRaisesRegex(OSError, "below 5 GiB"):
                    storage.check_disk_preflight()

    def test_startup_messages_report_the_effective_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            artifact = runtime_limits.artifact_limit_startup_message()
            deployment = runtime_limits.deployment_limits_startup_message()
        self.assertEqual(
            artifact,
            "artifact cap: 4096 MiB (WORKBENCH_MAX_ARTIFACT_MIB, default 4096, min 64 MiB, unbounded)",
        )
        self.assertIn("data budget 10 GiB", deployment)
        self.assertIn("free floor 20 GiB", deployment)
        self.assertIn("WORKBENCH_RSS_LIMIT_MIB", deployment)

    def test_child_command_uses_configured_rss_and_log_limits(self) -> None:
        with patch.dict(os.environ, {"WORKBENCH_RSS_LIMIT_MIB": "1024",
                                     "WORKBENCH_MAX_LOG_MIB": "7"}):
            command = supervisor.child_command(Path("/tmp/run"), 120)
        self.assertEqual(command[command.index("--max-rss-mib") + 1], "1024")
        self.assertEqual(command[command.index("--max-log-bytes") + 1], str(7 * 1024**2))


class CapabilitiesExposure(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_are_visible(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            body = await backend_main.capabilities()
        limits = body["runtimeLimits"]
        self.assertEqual(limits["artifactLimitBytes"], 4294967296)
        self.assertEqual(limits["artifactLimitMiB"], 4096)
        self.assertEqual(limits["artifactLimitSource"], "default")
        self.assertEqual(limits["artifactLimitEnvironmentVariable"], "WORKBENCH_MAX_ARTIFACT_MIB")
        self.assertEqual(limits["artifactLimitDefaultMiB"], 4096)
        self.assertEqual(limits["artifactLimitMinimumMiB"], 64)
        self.assertIsNone(limits["artifactLimitMaximumMiB"])
        self.assertEqual(limits["maximumDataGiB"], 10)
        self.assertEqual(limits["minimumFreeGiB"], 20)
        self.assertEqual(limits["rssLimitMiB"], 8192)
        self.assertEqual(limits["maximumLogMiB"], 4)
        run = body["resourcePolicy"]["run"]
        self.assertEqual(run["maximumArtifactBytes"], 4294967296)
        self.assertEqual(run["maximumResultArchiveBytes"], 4294967296)
        self.assertEqual(run["artifactLimitMiB"], 4096)
        self.assertEqual(run["artifactPreflight"]["capBytes"], 4294967296)
        self.assertEqual(run["artifactPreflight"]["capMiB"], 4096)
        self.assertEqual(run["storage"]["maximumWorkbenchBytes"], 10 * 1024**3)
        self.assertEqual(run["storage"]["minimumFreeBytes"], 20 * 1024**3)
        self.assertEqual(run["rssTerminationMiB"], 8192)
        self.assertEqual(run["maximumCombinedStdoutStderrBytes"], 4 * 1024**2)

    async def test_env_overrides_are_visible(self) -> None:
        with patch.dict(os.environ, {
            "WORKBENCH_MAX_ARTIFACT_MIB": "16384",
            "WORKBENCH_MAX_DATA_GIB": "3",
            "WORKBENCH_MIN_FREE_GIB": "5",
            "WORKBENCH_RSS_LIMIT_MIB": "1024",
            "WORKBENCH_MAX_LOG_MIB": "8",
        }):
            body = await backend_main.capabilities()
        limits = body["runtimeLimits"]
        self.assertEqual(limits["artifactLimitMiB"], 16384)
        self.assertEqual(limits["artifactLimitBytes"], 16384 * 1024**2)
        self.assertEqual(limits["artifactLimitSource"], "env")
        self.assertIsNone(limits["artifactLimitMaximumMiB"])
        self.assertEqual(limits["maximumDataGiB"], 3)
        self.assertEqual(limits["minimumFreeGiB"], 5)
        self.assertEqual(limits["rssLimitMiB"], 1024)
        self.assertEqual(limits["maximumLogMiB"], 8)
        run = body["resourcePolicy"]["run"]
        self.assertEqual(run["maximumArtifactBytes"], 16384 * 1024**2)
        self.assertEqual(run["maximumResultArchiveBytes"], 16384 * 1024**2)
        self.assertEqual(run["artifactPreflight"]["capBytes"], 16384 * 1024**2)
        self.assertEqual(run["storage"]["maximumWorkbenchBytes"], 3 * 1024**3)
        self.assertEqual(run["storage"]["minimumFreeBytes"], 5 * 1024**3)
        self.assertEqual(run["rssTerminationMiB"], 1024)
        self.assertEqual(run["maximumCombinedStdoutStderrBytes"], 8 * 1024**2)

    async def test_invalid_values_fall_back_and_report_default_source(self) -> None:
        with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "not-a-number",
                                     "WORKBENCH_MAX_DATA_GIB": "0",
                                     "WORKBENCH_RSS_LIMIT_MIB": "511"}):
            with self.assertLogs("backend.runtime_limits", level="WARNING"):
                body = await backend_main.capabilities()
        self.assertEqual(body["runtimeLimits"]["artifactLimitMiB"], 4096)
        self.assertEqual(body["runtimeLimits"]["artifactLimitSource"], "default")
        self.assertEqual(body["runtimeLimits"]["maximumDataGiB"], 10)
        self.assertEqual(body["runtimeLimits"]["rssLimitMiB"], 8192)


class StartupLog(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_logs_the_effective_artifact_cap_once(self) -> None:
        with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "16384"}):
            with self.assertLogs("uvicorn.error", level="INFO") as logs:
                async with backend_main.app.router.lifespan_context(backend_main.app):
                    pass
        matches = [line for line in logs.output
                   if "artifact cap: 16384 MiB (WORKBENCH_MAX_ARTIFACT_MIB=16384" in line]
        self.assertEqual(len(matches), 1, logs.output)
        self.assertTrue(any("deployment limits:" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
