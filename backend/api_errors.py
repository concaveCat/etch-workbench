"""Shared 422/JSON error helpers for the FastAPI routes.

``main.py`` keeps thin aliases (``_validation_error``, ``_project_or_422``) so
every existing route and test import keeps working, while extracted modules
(``packages``) can raise the same shapes without importing ``main``.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .models import Project
from .validation import ProjectInputError, validate_project


def validation_error_response(exc: ValidationError) -> JSONResponse:
    errors = []
    for item in exc.errors():
        path = ""
        for part in item["loc"]:
            path += f"[{part}]" if isinstance(part, int) else (str(part) if not path else f".{part}")
        errors.append({"path": path or "$", "message": item["msg"]})
    return JSONResponse(status_code=422, content={"detail": errors})


def project_or_422(payload: Any, *, kind: str = "project") -> tuple[Project, dict[str, Any], list[str]]:
    try:
        project, warnings = validate_project(payload, kind=kind)
    except ProjectInputError as exc:
        raise HTTPException(
            status_code=422, detail=[{"path": issue.path, "message": issue.message} for issue in exc.issues]
        ) from exc
    return project, project.model_dump(mode="json", exclude_none=True), warnings
