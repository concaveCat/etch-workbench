"""P3-3 basic calibration: CSV datasets, least squares and Nelder-Mead."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

MODELS = {
    "scale": {"parameters": ["a"], "method": "linear"},
    "scale_offset": {"parameters": ["a", "b"], "method": "linear"},
    "quadratic": {"parameters": ["a", "b", "c"], "method": "linear"},
    "power": {"parameters": ["a", "b"], "method": "nelder-mead"},
    "power_offset": {"parameters": ["a", "b", "c"], "method": "nelder-mead"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_calibration_csv(text: str) -> list[dict[str, Any]]:
    if len(text) > 2_000_000:
        raise ValueError("calibration CSV exceeds the 2 MB limit")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("calibration CSV has no header")
    fields = {name.strip().lower(): name for name in reader.fieldnames}
    if "measured" not in fields or "simulated" not in fields:
        raise ValueError("calibration CSV requires measured and simulated columns")
    rows = []
    for index, raw in enumerate(reader, start=2):
        if len(rows) >= 10_000:
            raise ValueError("calibration CSV exceeds the 10,000 row limit")
        try:
            measured = float(raw[fields["measured"]])
            simulated = float(raw[fields["simulated"]])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"calibration CSV row {index} has a non-numeric value") from exc
        if not math.isfinite(measured) or not math.isfinite(simulated):
            raise ValueError(f"calibration CSV row {index} must be finite")
        def value(name: str, default: str = "") -> str:
            key = fields.get(name)
            if not key:
                return default
            item = raw.get(key, default)
            return default if item is None else str(item)

        weight = 1.0
        if value("weight").strip():
            try:
                weight = float(value("weight"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"calibration CSV row {index} has a non-numeric weight") from exc
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"calibration CSV row {index} weight must be positive")
        split = value("split", "train").strip().lower()
        if split not in {"train", "validation"}:
            raise ValueError(f"calibration CSV row {index} split must be train or validation")
        rows.append({
            "index": index - 1,
            "condition": value("condition"),
            "measured": measured, "simulated": simulated, "weight": weight, "split": split,
        })
    if not rows:
        raise ValueError("calibration CSV has no data rows")
    return rows


def _design(values: list[float], model: str) -> list[list[float]]:
    if model == "scale":
        return [[value] for value in values]
    if model == "scale_offset":
        return [[value, 1.0] for value in values]
    if model == "quadratic":
        return [[value * value, value, 1.0] for value in values]
    raise ValueError("model is not linear")


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            raise ValueError("calibration least-squares system is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        factor = augmented[column][column]
        augmented[column] = [value / factor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * augmented[column][index] for index, value in enumerate(augmented[row])]
    return [augmented[index][size] for index in range(size)]


def _least_squares(rows: list[dict[str, Any]], model: str) -> list[float]:
    design = _design([float(row["simulated"]) for row in rows], model)
    size = len(design[0])
    matrix = [[0.0] * size for _ in range(size)]
    vector = [0.0] * size
    for row, features in zip(rows, design):
        weight = float(row["weight"])
        measured = float(row["measured"])
        for i in range(size):
            vector[i] += weight * features[i] * measured
            for j in range(size):
                matrix[i][j] += weight * features[i] * features[j]
    return _solve(matrix, vector)


def _predict(model: str, parameters: list[float], value: float) -> float:
    a = parameters[0] if parameters else 0.0
    if model == "scale":
        return a * value
    b = parameters[1] if len(parameters) > 1 else 0.0
    if model == "scale_offset":
        return a * value + b
    if model == "quadratic":
        c = parameters[2] if len(parameters) > 2 else 0.0
        return a * value * value + b * value + c
    if model in {"power", "power_offset"} and value == 0 and b < 0:
        return math.inf
    if model == "power":
        return a * (abs(value) ** b) * (1.0 if value >= 0 else -1.0)
    if model == "power_offset":
        c = parameters[2] if len(parameters) > 2 else 0.0
        return a * (abs(value) ** b) * (1.0 if value >= 0 else -1.0) + c
    raise ValueError("unknown calibration model")


def _sse(rows: list[dict[str, Any]], model: str, parameters: list[float]) -> float:
    total = 0.0
    for row in rows:
        if model in {"power", "power_offset"} and parameters[0] == 0:
            return math.inf
        try:
            prediction = _predict(model, parameters, float(row["simulated"]))
        except (OverflowError, ValueError, ZeroDivisionError):
            return math.inf
        if not math.isfinite(prediction):
            return math.inf
        total += float(row["weight"]) * (prediction - float(row["measured"])) ** 2
    return total


def _nelder_mead(rows: list[dict[str, Any]], model: str, initial: list[float]) -> tuple[list[float], int]:
    size = len(initial)
    simplex = [initial[:]]
    for index in range(size):
        point = initial[:]
        point[index] += 0.15 if abs(point[index]) < 1e-6 else max(1e-3, abs(point[index]) * 0.15)
        simplex.append(point)
    values = [_sse(rows, model, point) for point in simplex]
    for iteration in range(1, 2001):
        order = sorted(range(size + 1), key=lambda index: values[index])
        simplex = [simplex[index] for index in order]
        values = [values[index] for index in order]
        if abs(values[-1] - values[0]) <= 1e-12 * (1.0 + abs(values[0])) and max(
            math.dist(simplex[-1], point) for point in simplex[:-1]
        ) <= 1e-9:
            return simplex[0], iteration
        centroid = [sum(simplex[index][dimension] for index in range(size)) / size for dimension in range(size)]
        reflected = [centroid[dimension] + (centroid[dimension] - simplex[-1][dimension]) for dimension in range(size)]
        reflected_value = _sse(rows, model, reflected)
        if values[0] <= reflected_value < values[-2]:
            simplex[-1], values[-1] = reflected, reflected_value
            continue
        if reflected_value < values[0]:
            expanded = [centroid[dimension] + 2.0 * (reflected[dimension] - centroid[dimension]) for dimension in range(size)]
            expanded_value = _sse(rows, model, expanded)
            simplex[-1], values[-1] = (expanded, expanded_value) if expanded_value < reflected_value else (reflected, reflected_value)
            continue
        contracted = [centroid[dimension] + 0.5 * (simplex[-1][dimension] - centroid[dimension]) for dimension in range(size)]
        contracted_value = _sse(rows, model, contracted)
        if contracted_value < values[-1]:
            simplex[-1], values[-1] = contracted, contracted_value
            continue
        for index in range(1, size + 1):
            simplex[index] = [simplex[0][dimension] + 0.5 * (simplex[index][dimension] - simplex[0][dimension]) for dimension in range(size)]
            values[index] = _sse(rows, model, simplex[index])
    return simplex[0], 2000


def _residuals(rows: list[dict[str, Any]], model: str, parameters: list[float]) -> dict[str, Any]:
    errors = []
    for row in rows:
        prediction = _predict(model, parameters, float(row["simulated"]))
        if not math.isfinite(prediction):
            raise ValueError("calibration model produced a non-finite prediction; use positive simulated values")
        errors.append(prediction - float(row["measured"]))
    if not errors:
        return {"n": 0, "rmse": None, "mae": None, "maxAbs": None, "meanError": None}
    return {
        "n": len(errors),
        "rmse": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "mae": sum(abs(value) for value in errors) / len(errors),
        "maxAbs": max(abs(value) for value in errors),
        "meanError": sum(errors) / len(errors),
    }


def fit_rows(rows: list[dict[str, Any]], model: str, method: str) -> dict[str, Any]:
    if model not in MODELS:
        raise ValueError(f"unknown calibration model {model!r}")
    if method not in {"least-squares", "nelder-mead"}:
        raise ValueError("calibration method must be least-squares or nelder-mead")
    if MODELS[model]["method"] == "nelder-mead" and method == "least-squares":
        raise ValueError("power models require the nelder-mead method")
    train = [row for row in rows if row["split"] == "train"]
    validation = [row for row in rows if row["split"] == "validation"]
    if not train:
        raise ValueError("calibration requires at least one training row")
    if model == "power" and any(row["simulated"] <= 0 for row in train + validation):
        raise ValueError("power calibration requires positive simulated values; use power_offset or scale_offset for zero points")
    if MODELS[model]["method"] == "linear" and method == "least-squares":
        parameters = _least_squares(train, model)
        iterations = 0
    else:
        initial = [1.0, 1.0, 0.0][:len(MODELS[model]["parameters"])]
        parameters, iterations = _nelder_mead(train, model, initial)
    return {
        "schemaVersion": 1,
        "model": model,
        "method": method,
        "parameterNames": MODELS[model]["parameters"],
        "parameters": parameters,
        "iterations": iterations,
        "trainResiduals": _residuals(train, model, parameters),
        "validationResiduals": _residuals(validation, model, parameters),
        "validationStatus": "validated" if validation else "not_validated",
        "trainCount": len(train),
        "validationCount": len(validation),
        "assumptions": "independent observations; weighted least squares; validation split declared in CSV",
    }


def create_dataset(name: str, csv_text: str) -> dict[str, Any]:
    rows = parse_calibration_csv(csv_text)
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    return {
        "schemaVersion": 1,
        "datasetId": f"ds_{uuid.uuid4().hex[:16]}",
        "name": name,
        "sha256": digest,
        "rows": rows,
        "trainCount": sum(1 for row in rows if row["split"] == "train"),
        "validationCount": sum(1 for row in rows if row["split"] == "validation"),
        "createdAt": _now(),
    }


def run_fit(dataset: dict[str, Any], model: str, method: str) -> dict[str, Any]:
    result = fit_rows(dataset["rows"], model, method)
    result.update({
        "fitId": f"fit_{uuid.uuid4().hex[:16]}",
        "datasetId": dataset["datasetId"],
        "datasetSha256": dataset["sha256"],
        "createdAt": _now(),
    })
    return result
