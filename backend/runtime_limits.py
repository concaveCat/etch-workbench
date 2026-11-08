"""Runtime limits shared by the API process and the bounded worker.

Limits come from ``settings.json`` in the data directory when present, with
``WORKBENCH_*`` environment variables acting as an outer boundary and the
compiled defaults as the final fallback. Settings are cached by file mtime, so
changes written by the API take effect without a restart. Invalid values fall
back to the documented default with a logged warning.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import settings as settings_store

LOGGER = logging.getLogger(__name__)

MIB = 1024 * 1024
GIB = 1024 * MIB

#: Per-run artifact/archive cap.
ARTIFACT_LIMIT_ENV = settings_store.ARTIFACT_LIMIT_ENV
DEFAULT_ARTIFACT_LIMIT_MIB = settings_store.DEFAULT_ARTIFACT_LIMIT_MIB
MIN_ARTIFACT_LIMIT_MIB = settings_store.MIN_ARTIFACT_LIMIT_MIB

#: Total application-data budget.
MAX_DATA_ENV = settings_store.MAX_DATA_ENV
DEFAULT_MAX_DATA_GIB = settings_store.DEFAULT_MAX_DATA_GIB
MIN_MAX_DATA_GIB = settings_store.MIN_MAX_DATA_GIB

#: Free-disk floor before accepting new data.
MIN_FREE_ENV = settings_store.MIN_FREE_ENV
DEFAULT_MIN_FREE_GIB = settings_store.DEFAULT_MIN_FREE_GIB
MIN_MIN_FREE_GIB = settings_store.MIN_MIN_FREE_GIB

#: Soft RSS polling watch for a supervised worker (not a cgroup limit).
RSS_LIMIT_ENV = settings_store.RSS_LIMIT_ENV
DEFAULT_RSS_LIMIT_MIB = settings_store.DEFAULT_RSS_LIMIT_MIB
MIN_RSS_LIMIT_MIB = settings_store.MIN_RSS_LIMIT_MIB

#: Per-run combined stdout/stderr cap.
MAX_LOG_ENV = settings_store.MAX_LOG_ENV
DEFAULT_MAX_LOG_MIB = settings_store.DEFAULT_MAX_LOG_MIB
MIN_MAX_LOG_MIB = settings_store.MIN_MAX_LOG_MIB

#: Hard per-step DenseCellSet construction limit. The native cell set is a
#: dense voxel grid: 2 million cells already needs roughly 0.7 GiB of worker
#: memory and a ~40 MiB VTU, so a larger request is an obvious configuration
#: error rather than something the artifact preflight should discover late.
#: The artifact cap is enforced separately and remains authoritative.
MAX_CELL_SET_CELLS = 2_000_000


def _effective(key: str) -> dict[str, Any]:
    return settings_store.effective_limit(key)


def _read_int(env: str, default: int, minimum: int) -> tuple[int, str, str | None]:
    """Return ``(value, source, raw)`` for a limit selected by env name."""
    key = next((name for name, spec in settings_store.LIMIT_SPECS.items()
                if spec["env"] == env), None)
    if key is None:
        raise KeyError(env)
    limit = _effective(key)
    return int(limit["value"]), str(limit["source"]), limit["envValue"]


def artifact_limit_mib() -> int:
    """Return the effective per-run artifact cap in MiB (default 4096, minimum 64)."""
    return int(_effective("artifactLimitMiB")["value"])


def artifact_limit_bytes() -> int:
    """Return the effective per-run artifact cap in bytes."""
    return artifact_limit_mib() * MIB


def artifact_limit_label() -> str:
    """Return the effective per-run artifact cap for user-facing messages."""
    return f"{artifact_limit_mib()} MiB"


def max_data_gib() -> int:
    """Return the effective total application-data budget in GiB (default 10, minimum 1)."""
    return int(_effective("maxDataGiB")["value"])


def max_data_bytes() -> int:
    return max_data_gib() * GIB


def min_free_gib() -> int:
    """Return the effective free-disk floor in GiB (default 20, minimum 0)."""
    return int(_effective("minFreeGiB")["value"])


def min_free_bytes() -> int:
    return min_free_gib() * GIB


def rss_limit_mib() -> int:
    """Return the effective soft supervised-worker RSS watch in MiB."""
    return int(_effective("rssLimitMiB")["value"])


def max_log_mib() -> int:
    """Return the effective per-run combined log cap in MiB."""
    return int(_effective("maxLogMiB")["value"])


def max_log_bytes() -> int:
    return max_log_mib() * MIB


def artifact_limit_status() -> dict[str, Any]:
    """Return the effective artifact cap and the metadata behind it."""
    limit = _effective("artifactLimitMiB")
    value = int(limit["value"])
    return {
        "artifactLimitBytes": value * MIB,
        "artifactLimitMiB": value,
        "artifactLimitSource": limit["source"],
        "artifactLimitEnvironmentVariable": ARTIFACT_LIMIT_ENV,
        "artifactLimitEnvironmentValue": limit["envValue"],
        "artifactLimitDefaultMiB": DEFAULT_ARTIFACT_LIMIT_MIB,
        "artifactLimitMinimumMiB": MIN_ARTIFACT_LIMIT_MIB,
        "artifactLimitMaximumMiB": None,
    }


def deployment_limit_status() -> dict[str, Any]:
    """Return every effective deployment limit and its configuration metadata."""
    status = artifact_limit_status()
    data = _effective("maxDataGiB")
    free = _effective("minFreeGiB")
    rss = _effective("rssLimitMiB")
    log = _effective("maxLogMiB")
    status.update({
        "maximumDataBytes": int(data["value"]) * GIB,
        "maximumDataGiB": int(data["value"]),
        "maximumDataSource": data["source"],
        "maximumDataEnvironmentVariable": MAX_DATA_ENV,
        "maximumDataEnvironmentValue": data["envValue"],
        "maximumDataDefaultGiB": DEFAULT_MAX_DATA_GIB,
        "maximumDataMinimumGiB": MIN_MAX_DATA_GIB,
        "maximumDataMaximumGiB": None,
        "minimumFreeBytes": int(free["value"]) * GIB,
        "minimumFreeGiB": int(free["value"]),
        "minimumFreeSource": free["source"],
        "minimumFreeEnvironmentVariable": MIN_FREE_ENV,
        "minimumFreeEnvironmentValue": free["envValue"],
        "minimumFreeDefaultGiB": DEFAULT_MIN_FREE_GIB,
        "minimumFreeMinimumGiB": MIN_MIN_FREE_GIB,
        "minimumFreeMaximumGiB": None,
        "rssLimitBytes": int(rss["value"]) * MIB,
        "rssLimitMiB": int(rss["value"]),
        "rssLimitSource": rss["source"],
        "rssLimitEnvironmentVariable": RSS_LIMIT_ENV,
        "rssLimitEnvironmentValue": rss["envValue"],
        "rssLimitDefaultMiB": DEFAULT_RSS_LIMIT_MIB,
        "rssLimitMinimumMiB": MIN_RSS_LIMIT_MIB,
        "rssLimitMaximumMiB": None,
        "maximumLogBytes": int(log["value"]) * MIB,
        "maximumLogMiB": int(log["value"]),
        "maximumLogSource": log["source"],
        "maximumLogEnvironmentVariable": MAX_LOG_ENV,
        "maximumLogEnvironmentValue": log["envValue"],
        "maximumLogDefaultMiB": DEFAULT_MAX_LOG_MIB,
        "maximumLogMinimumMiB": MIN_MAX_LOG_MIB,
        "maximumLogMaximumMiB": None,
    })
    return status


def _source_detail(env: str, source: str, raw: str | None) -> str:
    if source == "settings":
        return f"{env} overridden by settings.json"
    if source == "env":
        return f"{env}={raw}"
    if raw is not None:
        return f"{env}={raw!r} ignored"
    return env


def artifact_limit_startup_message() -> str:
    """Return the one-line startup summary for the effective artifact cap."""
    limit = _effective("artifactLimitMiB")
    value = int(limit["value"])
    detail = _source_detail(ARTIFACT_LIMIT_ENV, str(limit["source"]), limit["envValue"])
    return (f"artifact cap: {value} MiB ({detail}, "
            f"default {DEFAULT_ARTIFACT_LIMIT_MIB}, "
            f"min {MIN_ARTIFACT_LIMIT_MIB} MiB, unbounded)")


def deployment_limits_startup_message() -> str:
    """Return the one-line startup summary for the remaining deployment limits."""
    data = _effective("maxDataGiB")
    free = _effective("minFreeGiB")
    rss = _effective("rssLimitMiB")
    log = _effective("maxLogMiB")
    return ("deployment limits: "
            f"data budget {data['value']} GiB "
            f"({_source_detail(MAX_DATA_ENV, str(data['source']), data['envValue'])}); "
            f"free floor {free['value']} GiB "
            f"({_source_detail(MIN_FREE_ENV, str(free['source']), free['envValue'])}); "
            f"RSS watch {rss['value']} MiB "
            f"({_source_detail(RSS_LIMIT_ENV, str(rss['source']), rss['envValue'])}); "
            f"log cap {log['value']} MiB "
            f"({_source_detail(MAX_LOG_ENV, str(log['source']), log['envValue'])})")


def cpu_thread_limit() -> dict[str, int | str]:
    """Return the number of logical CPUs available to this process and its source."""
    affinity_getter = getattr(os, "sched_getaffinity", None)
    if affinity_getter is not None:
        try:
            affinity = affinity_getter(0)
        except OSError:
            affinity = None
        if affinity:
            return {"maximum": max(1, len(affinity)), "limitSource": "process CPU affinity"}

    return {"maximum": max(1, os.cpu_count() or 1), "limitSource": "os.cpu_count fallback"}


def available_cpu_threads() -> int:
    return int(cpu_thread_limit()["maximum"])
