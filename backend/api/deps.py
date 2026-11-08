"""Shared dependencies for extracted FastAPI routers.

The helpers read the live ``app.state`` and installed distribution metadata, so
route modules can use them without importing ``backend.main`` (which would be a
cycle).
"""

from __future__ import annotations

import importlib.metadata

from fastapi import Request

from ..supervisor import Supervisor


def supervisor(request: Request) -> Supervisor:
    return request.app.state.supervisor


def version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
