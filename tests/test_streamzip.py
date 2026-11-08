from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend import runtime_limits
from backend.streamzip import stream_zip, stream_zip_async


class StreamZipTests(unittest.TestCase):
    def test_streamed_archive_is_readable_without_a_staging_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project.json").write_text('{"schemaVersion":1}\n', encoding="utf-8")
            (root / "initial.vtk").write_text("tiny mesh payload\n", encoding="ascii")
            result = b"".join(stream_zip([("project.json", root / "project.json"), ("initial.vtk", root / "initial.vtk")]))
            with zipfile.ZipFile(io.BytesIO(result)) as archive:
                self.assertEqual(archive.testzip(), None)
                self.assertEqual(archive.namelist(), ["project.json", "initial.vtk"])
                self.assertEqual(archive.read("initial.vtk"), b"tiny mesh payload\n")

    def test_entry_names_allow_safe_subdirectories_and_reject_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "input"
            file.write_text("x", encoding="ascii")
            nested = b"".join(stream_zip([("intermediate/step/input.vtp", file)]))
            with zipfile.ZipFile(io.BytesIO(nested)) as archive:
                self.assertEqual(archive.namelist(), ["intermediate/step/input.vtp"])
                self.assertEqual(archive.read("intermediate/step/input.vtp"), b"x")
            for name in ("../input", "/tmp/input", "intermediate/../input", "intermediate//input",
                         "intermediate/./input", "C:/input", "\\\\server\\share"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    list(stream_zip([(name, file)]))

    def test_async_http_adapter_streams_zip_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project.json").write_text('{"schemaVersion":1}\n', encoding="utf-8")

            async def collect() -> bytes:
                return b"".join([chunk async for chunk in stream_zip_async([("project.json", root / "project.json")])])

            result = asyncio.run(collect())
            with zipfile.ZipFile(io.BytesIO(result)) as archive:
                self.assertEqual(archive.testzip(), None)
                self.assertEqual(archive.read("project.json"), b'{"schemaVersion":1}\n')

    def test_default_archive_limit_follows_the_configured_artifact_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            big = root / "big.bin"
            with big.open("wb") as stream:
                stream.truncate(640 * 1024**2)

            # The 4 GiB default accepts a >512 MiB entry; the pre-scan fires
            # before any payload is read, so this is a sparse file.
            iterator = stream_zip([("big.bin", big)])
            self.assertTrue(next(iterator).startswith(b"PK\x03\x04"))
            iterator.close()

            with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "512"}):
                with self.assertRaises(ValueError) as raised:
                    next(stream_zip([("big.bin", big)]))
                self.assertIn("512 MiB", str(raised.exception))
                self.assertEqual(runtime_limits.artifact_limit_bytes(), 512 * 1024**2)


if __name__ == "__main__":
    unittest.main()
