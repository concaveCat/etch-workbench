"""GPU UUID selection is stable, explicit, and represented in schema-7 hashes."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
import unittest

from backend.gpu_devices import resolve_gpu_device
from backend.hashing import requested_input_hash
from backend.validation import ProjectInputError, validate_project

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from run_gpu_singleparticle_coverage import make_generic_project


GPU_A = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"
GPU_B = "GPU-12345678-1234-1234-1234-123456789abc"


class GpuDeviceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inventory = {
            "status": "available",
            "preferredGpuDeviceId": GPU_A,
            "devices": [
                {"uuid": GPU_A, "name": "RTX device A", "pciBusId": "0000:01:00.0"},
                {"uuid": GPU_B, "name": "RTX device B", "pciBusId": "0000:02:00.0"},
            ],
        }

    def test_auto_uses_host_preferred_device_and_explicit_selection_switches(self) -> None:
        self.assertEqual(resolve_gpu_device(None, self.inventory)["uuid"], GPU_A)
        self.assertEqual(resolve_gpu_device(GPU_B, self.inventory)["uuid"], GPU_B)

    def test_unavailable_explicit_or_automatic_device_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "selected compute GPU"):
            resolve_gpu_device("GPU-deadbeef-1234-1234-1234-123456789abc", self.inventory)
        inventory = {"status": "unavailable", "preferredGpuDeviceId": None, "devices": []}
        with self.assertRaisesRegex(RuntimeError, "no CUDA-capable NVIDIA GPU"):
            resolve_gpu_device(None, inventory)

    def test_schema7_gpu_uuid_round_trips_in_hash_without_changing_auto_hash(self) -> None:
        project = make_generic_project(2, "etch", "none")
        project["solver"].pop("gpuDeviceId", None)
        automatic, _ = validate_project(project, kind="project")
        automatic_hash = requested_input_hash(automatic)

        first = copy.deepcopy(project)
        first["solver"]["gpuDeviceId"] = GPU_A
        parsed_first, _ = validate_project(first, kind="project")
        normalized_first = parsed_first.model_dump(mode="json", exclude_none=True)
        self.assertEqual(normalized_first["solver"]["gpuDeviceId"], GPU_A)
        self.assertEqual(requested_input_hash(parsed_first), requested_input_hash(validate_project(first, kind="project")[0]))

        second = copy.deepcopy(project)
        second["solver"]["gpuDeviceId"] = GPU_B
        parsed_second, _ = validate_project(second, kind="project")
        self.assertNotEqual(requested_input_hash(parsed_first), requested_input_hash(parsed_second))
        self.assertEqual(automatic_hash, requested_input_hash(validate_project(project, kind="project")[0]))

        malformed = copy.deepcopy(project)
        malformed["solver"]["gpuDeviceId"] = "0"
        with self.assertRaises(ProjectInputError):
            validate_project(malformed, kind="project")


if __name__ == "__main__":
    unittest.main()
