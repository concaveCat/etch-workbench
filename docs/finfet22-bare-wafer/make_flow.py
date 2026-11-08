# -*- coding: utf-8 -*-
"""Bare-wafer 22 nm FinFET demo.

The input GDS is used ONLY as the mask set (Fin layer 1, Gate 2, CB 4, M0 5).
The simulation starts from a flat Si wafer (geometry layer 7 mapped as a solid
substrate) and builds the fin/STI/gate topography with native process steps;
the genuinely missing / unsupported steps (lithography pattern transfer,
CMP) are geometry-only operations and are labelled as such.

Stages:
  front : pad oxide/nitride, fin hard mask, trench, STI fill/CMP, fin reveal,
          well implant/anneal/analysis
  gate  : front + gate oxide, poly, poly CMP, gate pattern, spacer,
          S/D epi + implant/anneal/analysis
  full  : gate + ILD/CMP, CB, dummy strip, HKMG, W gate fill/CMP, M0
"""
from __future__ import annotations

import hashlib
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
BASE = json.loads((HERE / "base-project.json").read_text())

GRID = 0.005
WAFER_TOP = 0.035
TRENCH_BOTTOM = -0.025
STI_TOP = 0.0
GATE_TOP = 0.115

CELLSET_WELL = {"height": 0.06, "substrateDepth": 0.02, "coverMaterial": "GAS",
                "periodic": False, "format": "both"}
CELLSET_SD = {"height": 0.13, "substrateDepth": 0.03, "coverMaterial": "GAS",
              "periodic": False, "format": "both"}


def project(name: str, steps: list[dict]) -> dict:
    p = json.loads(json.dumps(BASE))
    p["name"] = name
    p["domain"] = {"gridDelta": GRID, "xExtent": 0.5, "yExtent": 0.5,
                   "boundaryXY": "reflective"}
    p["geometry"]["assetId"] = hashlib.sha256((HERE / "finfet22_05.gds").read_bytes()).hexdigest()
    p["geometry"]["fileName"] = "finfet22_05.gds"
    p["geometry"]["structure"] = "FINFET22"
    p["geometry"]["unitScale"] = 1.0
    p["geometry"]["layers"] = [
        {"layer": 7, "material": "Si", "baseHeight": -0.045, "height": 0.080,
         "mask": False},
    ]
    p["steps"] = steps
    p["outputSettings"]["surfaceMeshVtk"] = False
    p["outputSettings"]["nativeMetadata"] = False
    return p


def deposit(step_id: str, name: str, material: str, thickness: float,
            rate: float | None = None) -> dict:
    rate = rate if rate is not None else thickness
    return {"id": step_id, "name": name, "enabled": True, "operation": "deposit",
            "model": "isotropic", "rateMode": "uniform", "rateMagnitude": rate,
            "depositMaterial": material, "duration": 1.0}


def directional(step_id: str, name: str, material: str, thickness: float,
                rate: float, isotropic: float = 0.0,
                protect: list[str] | None = None) -> dict:
    return {"id": step_id, "name": name, "enabled": True, "operation": "etch",
            "model": "directional", "duration": thickness / rate,
            "rateSets": [{"direction": [0.0, 0.0, -1.0],
                          "directionalVelocity": rate,
                          "isotropicVelocity": isotropic,
                          "maskMaterials": protect or [],
                          "calculateVisibility": True}]}


def selective_etch(step_id: str, name: str, material: str, thickness: float,
                   rate: float, protect: list[str]) -> dict:
    # Unlisted materials default to rateMagnitude, not zero: every other
    # material present in the stack must be named explicitly.
    rates = {name: 0.0 for name in protect}
    rates[material] = rate
    return {"id": step_id, "name": name, "enabled": True, "operation": "etch",
            "model": "isotropic", "rateMode": "material",
            "rateMagnitude": rate, "materialRates": rates,
            "duration": thickness / rate}


def gds_boolean(step_id: str, name: str, layer: int, operation: str,
                retained: str, base: float, height: float) -> dict:
    return {"id": step_id, "name": name, "enabled": True, "model": "gdsBoolean",
            "booleanOperation": operation, "applyToAll": False,
            "gdsMask": {"layer": layer, "datatype": 0, "unitScale": 1.0,
                        "position": [0, 0, 0], "baseHeight": base,
                        "height": height, "retainedRegion": retained}}


def planarize(step_id: str, name: str, cutoff: float) -> dict:
    return {"id": step_id, "name": name, "enabled": True, "model": "planarize",
            "cutoffHeight": cutoff}


def front_steps() -> list[dict]:
    return [
        deposit("bw_pad_ox", "Pad oxide hard mask 30 nm (native SiO2 deposition)", "SiO2", 0.030),
        gds_boolean("bw_fin_mask", "Fin hard-mask pattern (GDS layer 1, geometry-only)",
                    1, "difference", "outside", 0.0, 0.20),
        directional("bw_trench_etch", "STI trench directional Si etch 60 nm (native)", "Si",
                    0.060, 0.050, isotropic=0.0005, protect=["SiO2"]),
        deposit("bw_sti_fill", "STI trench fill 300 nm (native SiO2 deposition)", "SiO2",
                0.300, rate=0.100),
        planarize("bw_sti_cmp", "STI CMP to pad-oxide top 0.065 (geometry-only)", 0.065),
        selective_etch("bw_sti_recess", "STI recess 65 nm / pad-oxide strip (native selective SiO2 etch)",
                       "SiO2", 0.065, 0.050, protect=["Si"]),
    ]


def gate_steps() -> list[dict]:
    return [
        deposit("bw_gate_ox", "Gate oxide 2 nm (native SiO2 deposition)", "SiO2", 0.002),
        deposit("bw_poly_dep", "Dummy PolySi gate 120 nm (native deposition)", "PolySi",
                0.120),
        planarize("bw_poly_cmp", "Dummy gate CMP flat at 0.115 (geometry-only)", 0.115),
        gds_boolean("bw_gate_pat", "Gate pattern difference/outside (GDS layer 2, geometry-only)",
                    2, "difference", "outside", 0.000, 0.150),
        deposit("bw_spacer_dep", "SiN spacer 10 nm (native deposition)", "Si3N4", 0.010),
        directional("bw_spacer_etch", "Spacer directional etch (native)", "Si3N4",
                    0.014, 0.020, isotropic=0.001,
                    protect=["Si", "SiO2", "PolySi"]),
        {"id": "bw_sd_epi", "name": "S/D SiGe selective epitaxy 15 nm (native)", "enabled": True,
         "operation": "deposit", "model": "specialized", "nativeModel": "selectiveEpitaxy",
         "duration": 0.3, "depositMaterial": "SiGe",
         "nativeParameters": {"materialRates": {"Si": 0.05, "SiGe": 0.05},
                              "rate111": 0.5, "rate100": 1.0,
                              "nvFactors": [0.5, 1.0, 0.5]}},
    ]


def back_steps() -> list[dict]:
    return [
        deposit("bw_ild_dep", "ILD SiO2 150 nm (native deposition)", "SiO2", 0.150),
        planarize("bw_ild_cmp", "ILD CMP to dummy gate top 0.115 (geometry-only)", 0.115),
        gds_boolean("bw_cb_open", "CB contact opening (GDS layer 4, geometry-only)",
                    4, "difference", "inside", 0.040, 0.220),
        {"id": "bw_poly_strip", "name": "Dummy PolySi strip (native material removal)",
         "enabled": True, "model": "removeMaterial", "material": "PolySi"},
        deposit("bw_il_ald", "Gate IL SiO2 1 nm (native ALD/deposition)", "SiO2", 0.001),
        deposit("bw_hfo2_ald", "HfO2 ALD 2 nm (native deposition)", "HfO2", 0.002),
        deposit("bw_tin_ald", "TiN ALD 3 nm (native deposition)", "TiN", 0.003),
        deposit("bw_w_gate", "W gate fill 70 nm (native deposition)", "W", 0.070),
        planarize("bw_w_gate_cmp", "W gate CMP to 0.115 (geometry-only)", 0.115),
        gds_boolean("bw_m0_pattern", "M0 local interconnect (GDS layer 5, geometry-only)",
                    5, "union", "inside", 0.115, 0.030),
    ]


def main() -> None:
    front = front_steps()
    gate = front + gate_steps()
    full = gate + back_steps()
    for name, steps in (("flow_front", front), ("flow_gate", gate), ("flow_full", full)):
        p = project(f"22 nm FinFET bare-wafer demo ({name})", steps)
        out = HERE / f"{name}.json"
        out.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n")
        print(out.name, len(steps), "steps")


if __name__ == "__main__":
    main()
