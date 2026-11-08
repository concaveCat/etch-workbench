"""Persistent deployment settings shared by the API process and the worker.

Operators can override the compiled defaults through ``settings.json`` in the
workbench data directory. The ``WORKBENCH_*`` environment variables remain the
outer boundary: for a byte/size cap they act as a hard upper bound, and for the
free-disk floor they act as a hard lower bound. Settings are re-read per call
(cached by file mtime) so a write through ``PUT /api/settings`` takes effect
without restarting the service.

This module deliberately does not import :mod:`backend.storage`; storage
already imports :mod:`backend.runtime_limits`, and keeping the settings store
independent avoids an import cycle. The path and atomic writer are therefore
implemented locally.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

# Keep the historical log channel: deployments already watch
# ``backend.runtime_limits`` for invalid limit configuration.
LOGGER = logging.getLogger("backend.runtime_limits")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

#: Stable response/store ordering.
LIMIT_ORDER = (
    "maxDataGiB",
    "artifactLimitMiB",
    "rssLimitMiB",
    "maxLogMiB",
    "minFreeGiB",
)

#: Defaults for newly created projects. Unlike the limits above these are not
#: environment-capped; they only seed a new project's solver settings.
DEFAULT_ORDER = ("solverTimeoutSeconds",)

DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    "solverTimeoutSeconds": {
        "default": 600,
        "minimum": 5,
        "maximum": 86400,
        "unit": "s",
        "label": "新项目求解超时默认值",
    },
}

#: ``kind`` is ``upper`` for size caps (env is a hard maximum) and ``floor`` for
#: the free-disk minimum (env is a hard minimum).
LIMIT_SPECS: dict[str, dict[str, Any]] = {
    "maxDataGiB": {
        "env": "WORKBENCH_MAX_DATA_GIB",
        "default": 10,
        "minimum": 1,
        "maximum": None,
        "kind": "upper",
        "unit": "GiB",
        "label": "数据目录总预算",
    },
    "artifactLimitMiB": {
        "env": "WORKBENCH_MAX_ARTIFACT_MIB",
        "default": 4096,
        "minimum": 64,
        "maximum": None,
        "kind": "upper",
        "unit": "MiB",
        "label": "单 run artifact 上限",
    },
    "rssLimitMiB": {
        "env": "WORKBENCH_RSS_LIMIT_MIB",
        "default": 8192,
        "minimum": 512,
        "maximum": None,
        "kind": "upper",
        "unit": "MiB",
        "label": "worker RSS 软上限",
    },
    "maxLogMiB": {
        "env": "WORKBENCH_MAX_LOG_MIB",
        "default": 4,
        "minimum": 1,
        "maximum": None,
        "kind": "upper",
        "unit": "MiB",
        "label": "单 run 日志上限",
    },
    "minFreeGiB": {
        "env": "WORKBENCH_MIN_FREE_GIB",
        "default": 20,
        "minimum": 0,
        "maximum": None,
        "kind": "floor",
        "unit": "GiB",
        "label": "最小空闲磁盘",
    },
}

# Backwards-compatible names consumed by backend.runtime_limits.
ARTIFACT_LIMIT_ENV = LIMIT_SPECS["artifactLimitMiB"]["env"]
DEFAULT_ARTIFACT_LIMIT_MIB = LIMIT_SPECS["artifactLimitMiB"]["default"]
MIN_ARTIFACT_LIMIT_MIB = LIMIT_SPECS["artifactLimitMiB"]["minimum"]

MAX_DATA_ENV = LIMIT_SPECS["maxDataGiB"]["env"]
DEFAULT_MAX_DATA_GIB = LIMIT_SPECS["maxDataGiB"]["default"]
MIN_MAX_DATA_GIB = LIMIT_SPECS["maxDataGiB"]["minimum"]

MIN_FREE_ENV = LIMIT_SPECS["minFreeGiB"]["env"]
DEFAULT_MIN_FREE_GIB = LIMIT_SPECS["minFreeGiB"]["default"]
MIN_MIN_FREE_GIB = LIMIT_SPECS["minFreeGiB"]["minimum"]

RSS_LIMIT_ENV = LIMIT_SPECS["rssLimitMiB"]["env"]
DEFAULT_RSS_LIMIT_MIB = LIMIT_SPECS["rssLimitMiB"]["default"]
MIN_RSS_LIMIT_MIB = LIMIT_SPECS["rssLimitMiB"]["minimum"]

MAX_LOG_ENV = LIMIT_SPECS["maxLogMiB"]["env"]
DEFAULT_MAX_LOG_MIB = LIMIT_SPECS["maxLogMiB"]["default"]
MIN_MAX_LOG_MIB = LIMIT_SPECS["maxLogMiB"]["minimum"]

#: Data-directory children counted as application data (mirrors storage.py).
STORAGE_DIRECTORIES = (
    ("runs", "runs", None),
    ("projects", "projects", ".json"),
    ("domain-states", "domainStates", ".vpsd"),
    ("gds-assets", "gdsAssets", ".gds"),
    ("csv-assets", "csvAssets", ".csv"),
)

_WARNED_INVALID_ENV: set[tuple[str, str]] = set()
_WARNED_SETTINGS: set[tuple[str, str, str]] = set()
_WARNED_MISSING: set[str] = set()

# Cache the parsed settings file keyed by path and file signature. The mtime is
# the primary key; size/inode guard against coarse filesystem timestamps.
_RAW_CACHE: dict[str, tuple[tuple[int, int, int] | None, dict[str, Any]]] = {}


class SettingsValidationError(Exception):
    """Validation failure carrying the API's ``detail`` items."""

    def __init__(self, issues: list[dict[str, str]]):
        super().__init__("invalid settings")
        self.issues = issues


def data_directory() -> Path:
    """Return the active workbench data directory without creating it."""
    configured = os.environ.get("VIENNAPS_WORKBENCH_DATA_DIR")
    base = Path(configured).expanduser() if configured and configured.strip() else REPO_ROOT / "var"
    try:
        return base.resolve()
    except OSError:
        return base.absolute()


def settings_path() -> Path:
    return data_directory() / SETTINGS_FILENAME


def _env_limit(spec: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return ``(valid_env_value, raw_env_value)`` for one limit spec."""
    env = spec["env"]
    raw = os.environ.get(env)
    if raw is None:
        return None, None
    text = raw.strip()
    try:
        value = int(text, 10)
    except ValueError:
        value = None
    if value is not None and value >= spec["minimum"]:
        return value, raw
    if (env, raw) not in _WARNED_INVALID_ENV:
        _WARNED_INVALID_ENV.add((env, raw))
        LOGGER.warning(
            "%s=%r is invalid (expected an integer >= %d); using the default %d",
            env, raw, spec["minimum"], spec["default"],
        )
    return None, raw


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def load_stored_settings() -> dict[str, dict[str, Any]]:
    """Return ``{"limits": {...}, "defaults": {...}}`` from settings.json.

    The parsed file is cached by path and file signature. Files written before
    ``defaults`` existed load with an empty defaults object.
    """
    path = settings_path()
    key = str(path)
    signature = _signature(path)
    cached = _RAW_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return {"limits": dict(cached[1]["limits"]), "defaults": dict(cached[1]["defaults"])}
    if signature is None:
        if key not in _WARNED_MISSING:
            _WARNED_MISSING.add(key)
            LOGGER.warning("settings file %s is missing; using environment/default limits", path)
        _RAW_CACHE[key] = (None, {"limits": {}, "defaults": {}})
        return {"limits": {}, "defaults": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
        limits = payload.get("limits")
        if not isinstance(limits, dict):
            raise ValueError("expected an object with a 'limits' object")
        defaults = payload.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError("expected 'defaults' to be an object")
    except (OSError, ValueError, TypeError) as exc:
        marker = (key, str(signature), str(exc))
        if marker not in _WARNED_SETTINGS:
            _WARNED_SETTINGS.add(marker)
            LOGGER.warning("settings file %s is invalid (%s); using environment/default limits", path, exc)
        _RAW_CACHE[key] = (signature, {"limits": {}, "defaults": {}})
        return {"limits": {}, "defaults": {}}
    stored = {"limits": dict(limits), "defaults": dict(defaults)}
    _RAW_CACHE[key] = (signature, stored)
    return {"limits": dict(stored["limits"]), "defaults": dict(stored["defaults"])}


def load_stored_limits() -> dict[str, Any]:
    """Return the raw ``limits`` object from settings.json (cached by mtime)."""
    return load_stored_settings()["limits"]


def applicable_limits() -> dict[str, int]:
    """Return stored overrides that are valid under the current environment."""
    stored = load_stored_limits()
    applicable: dict[str, int] = {}
    for key, value in stored.items():
        spec = LIMIT_SPECS.get(key)
        if spec is None:
            continue
        reason = None
        if isinstance(value, bool) or not isinstance(value, int):
            reason = "not an integer"
        elif value < spec["minimum"]:
            reason = f"below the minimum {spec['minimum']}"
        else:
            env_value, _ = _env_limit(spec)
            if env_value is not None:
                if spec["kind"] == "upper" and value > env_value:
                    reason = f"above the {spec['env']} cap {env_value}"
                elif spec["kind"] == "floor" and value < env_value:
                    reason = f"below the {spec['env']} floor {env_value}"
        if reason is not None:
            marker = (str(settings_path()), repr(value), reason)
            if marker not in _WARNED_SETTINGS:
                _WARNED_SETTINGS.add(marker)
                LOGGER.warning("ignoring settings value %s=%r (%s)", key, value, reason)
            continue
        applicable[key] = value
    return applicable


def effective_limit(key: str) -> dict[str, Any]:
    """Return the effective value and precedence metadata for one limit."""
    spec = LIMIT_SPECS[key]
    stored = applicable_limits()
    env_value, env_raw = _env_limit(spec)
    if key in stored:
        value, source = stored[key], "settings"
    elif env_value is not None:
        value, source = env_value, "env"
    else:
        value, source = spec["default"], "default"
    maximum = env_value if spec["kind"] == "upper" and env_value is not None else None
    return {
        "value": value,
        "source": source,
        "default": spec["default"],
        "minimum": spec["minimum"],
        "maximum": maximum,
        "envVariable": spec["env"],
        "envValue": env_raw,
        "unit": spec["unit"],
        "label": spec["label"],
    }


def effective_value(key: str) -> int:
    return int(effective_limit(key)["value"])


def applicable_defaults() -> dict[str, int]:
    """Return stored defaults that are valid, warning about ignored values."""
    stored = load_stored_settings()["defaults"]
    applicable: dict[str, int] = {}
    for key, value in stored.items():
        spec = DEFAULT_SPECS.get(key)
        if spec is None:
            continue
        reason = None
        if isinstance(value, bool) or not isinstance(value, int):
            reason = "not an integer"
        elif value < spec["minimum"]:
            reason = f"below the minimum {spec['minimum']}"
        elif value > spec["maximum"]:
            reason = f"above the maximum {spec['maximum']}"
        if reason is not None:
            marker = (str(settings_path()), repr(value), reason)
            if marker not in _WARNED_SETTINGS:
                _WARNED_SETTINGS.add(marker)
                LOGGER.warning("ignoring settings default %s=%r (%s)", key, value, reason)
            continue
        applicable[key] = value
    return applicable


def effective_default(key: str) -> dict[str, Any]:
    """Return the effective value and metadata for one new-project default."""
    spec = DEFAULT_SPECS[key]
    stored = applicable_defaults()
    value = stored.get(key, spec["default"])
    return {
        "value": value,
        "source": "settings" if key in stored else "default",
        "default": spec["default"],
        "minimum": spec["minimum"],
        "maximum": spec["maximum"],
        "unit": spec["unit"],
        "label": spec["label"],
    }


def defaults_response() -> dict[str, int]:
    """Return the flat ``defaults`` object exposed by GET/PUT."""
    return {key: int(effective_default(key)["value"]) for key in DEFAULT_ORDER}


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def write_stored_settings(limits: dict[str, int], defaults: dict[str, int]) -> None:
    """Atomically persist limits and defaults in canonical order."""
    ordered_limits = {key: limits[key] for key in LIMIT_ORDER if key in limits}
    ordered_defaults = {key: defaults[key] for key in DEFAULT_ORDER if key in defaults}
    path = settings_path()
    _atomic_json(path, {
        "schemaVersion": SCHEMA_VERSION,
        "limits": ordered_limits,
        "defaults": ordered_defaults,
    })
    _RAW_CACHE.pop(str(path), None)


def write_stored_limits(limits: dict[str, int]) -> None:
    """Atomically persist only the provided limit overrides."""
    stored = load_stored_settings()["defaults"]
    write_stored_settings(limits, stored)


def _validate_value(key: str, value: Any, issues: list[dict[str, str]]) -> None:
    spec = LIMIT_SPECS[key]
    path = f"limits.{key}"
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append({"path": path, "message": "must be an integer or null"})
        return
    if value < spec["minimum"]:
        issues.append({"path": path, "message": f"must be >= {spec['minimum']} {spec['unit']}"})
        return
    env_value, _ = _env_limit(spec)
    if env_value is None:
        return
    if spec["kind"] == "upper" and value > env_value:
        issues.append({
            "path": path,
            "message": f"must be <= {spec['env']} ({env_value} {spec['unit']})",
        })
    elif spec["kind"] == "floor" and value < env_value:
        issues.append({
            "path": path,
            "message": f"must be >= {spec['env']} ({env_value} {spec['unit']})",
        })


def _validate_default(key: str, value: Any, issues: list[dict[str, str]]) -> None:
    spec = DEFAULT_SPECS[key]
    path = f"defaults.{key}"
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append({"path": path, "message": "must be an integer"})
        return
    if value < spec["minimum"]:
        issues.append({"path": path, "message": f"must be >= {spec['minimum']} {spec['unit']}"})
        return
    if value > spec["maximum"]:
        issues.append({"path": path, "message": f"must be <= {spec['maximum']} {spec['unit']}"})


def validate_update(payload: Any) -> dict[str, dict[str, int | None]]:
    """Validate a PUT body and return ``{"limits": ..., "defaults": ...}``."""
    if not isinstance(payload, dict):
        raise SettingsValidationError([
            {"path": "$", "message": "request body must be a JSON object"},
        ])
    issues: list[dict[str, str]] = []
    for key in payload:
        if key not in {"limits", "defaults", "schemaVersion"}:
            issues.append({"path": str(key), "message": "unknown field"})
    if "schemaVersion" in payload and payload["schemaVersion"] != SCHEMA_VERSION:
        issues.append({"path": "schemaVersion", "message": f"must be {SCHEMA_VERSION}"})
    if "limits" not in payload:
        limits: Any = {}
    else:
        limits = payload["limits"]
    if not isinstance(limits, dict):
        issues.append({"path": "limits", "message": "must be a JSON object"})
        limits = {}
    limit_updates: dict[str, int | None] = {}
    for key, value in limits.items():
        if key not in LIMIT_SPECS:
            issues.append({"path": f"limits.{key}", "message": "unknown limit"})
            continue
        if value is None:
            limit_updates[key] = None
            continue
        before = len(issues)
        _validate_value(key, value, issues)
        if len(issues) == before:
            limit_updates[key] = value
    if "defaults" not in payload:
        defaults: Any = {}
    else:
        defaults = payload["defaults"]
    if not isinstance(defaults, dict):
        issues.append({"path": "defaults", "message": "must be a JSON object"})
        defaults = {}
    default_updates: dict[str, int] = {}
    for key, value in defaults.items():
        if key not in DEFAULT_SPECS:
            issues.append({"path": f"defaults.{key}", "message": "unknown default"})
            continue
        before = len(issues)
        _validate_default(key, value, issues)
        if len(issues) == before:
            default_updates[key] = value
    if issues:
        raise SettingsValidationError(issues)
    return {"limits": limit_updates, "defaults": default_updates}


def apply_update(payload: Any) -> dict[str, Any]:
    """Validate, merge, persist, and return the full settings response."""
    updates = validate_update(payload)
    stored = load_stored_settings()
    merged_limits = dict(stored["limits"])
    for key, value in updates["limits"].items():
        if value is None:
            merged_limits.pop(key, None)
        else:
            merged_limits[key] = value
    merged_defaults = dict(stored["defaults"])
    merged_defaults.update(updates["defaults"])
    write_stored_settings(
        {key: value for key, value in merged_limits.items()
         if key in LIMIT_SPECS and isinstance(value, int)},
        {key: value for key, value in merged_defaults.items()
         if key in DEFAULT_SPECS and isinstance(value, int)},
    )
    return settings_response()


def _directory_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.name.endswith(".tmp"):
                continue
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _count_entries(path: Path, suffix: str | None) -> int:
    if not path.is_dir():
        return 0
    try:
        entries = list(path.iterdir())
    except OSError:
        return 0
    if suffix is None:
        return sum(1 for item in entries if item.is_dir())
    return sum(1 for item in entries
               if item.is_file() and item.name.endswith(suffix) and not item.name.startswith("."))


def _disk_usage(path: Path) -> tuple[int, int, int]:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    try:
        usage = shutil.disk_usage(candidate)
    except OSError:
        return 0, 0, 0
    return usage.total, usage.used, usage.free


def storage_summary() -> dict[str, Any]:
    """Return storage accounting for the public settings response."""
    base = data_directory()
    used = 0
    counts: dict[str, int] = {}
    for directory, count_key, suffix in STORAGE_DIRECTORIES:
        path = base / directory
        used += _directory_bytes(path)
        counts[count_key] = _count_entries(path, suffix)
    total, _used_disk, free = _disk_usage(base)
    return {
        "dataDirectory": str(base),
        "usedBytes": used,
        "freeBytes": free,
        "totalBytes": total,
        **counts,
    }


def _installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def settings_response() -> dict[str, Any]:
    """Build the GET/PUT response body."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "limits": {key: effective_limit(key) for key in LIMIT_ORDER},
        "defaults": defaults_response(),
        "storage": storage_summary(),
        "versions": {
            "viennaps": _installed_version("viennaps"),
            "viennals": _installed_version("viennals"),
        },
    }
