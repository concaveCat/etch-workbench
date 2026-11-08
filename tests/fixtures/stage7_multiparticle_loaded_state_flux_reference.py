"""Independent direct ViennaPS calculateFlux from a saved native Domain."""
from __future__ import annotations

import sys

from stage7_multiparticle_flux_reference import main


if len(sys.argv) != 6:
    raise SystemExit("usage: stage7_multiparticle_loaded_state_flux_reference.py PROJECT.json STEP_ID STATE.vpsd OUTPUT.json ENGINE")

project_path, step_id, state_path, output_path, engine = sys.argv[1:]
main(project_path, step_id, output_path, engine, state_path)
