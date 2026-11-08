"""Canonical encodings and stable workflow step identifiers."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any


STEP_ID_POLICY = "v1-base32-lower-nopad-20"


def canonical_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used by workflow hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_step_id(
    *,
    flow_id: str,
    module_instance_id_path: str,
    authored_step_id: str,
    iteration_key: str,
) -> str:
    """Build a stable step id from structural identity, never parameter values.

    Parameter edits must not change the id. The caller is responsible for
    keeping ``authored_step_id`` stable and for using instance ids rather than
    display names or array indices in ``module_instance_id_path``.
    """

    payload = {
        "v": 1,
        "flowId": flow_id,
        "moduleInstanceIdPath": module_instance_id_path,
        "authoredStepId": authored_step_id,
        "iterationKey": iteration_key,
    }
    digest = hashlib.sha256(canonical_bytes(payload)).digest()
    encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return f"s_{encoded[:20]}"
