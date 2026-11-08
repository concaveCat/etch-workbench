"""Independent direct ViennaPS GPU Process.apply from a saved native Domain."""
from __future__ import annotations

import sys

from stage7_multiparticle_reference import main


if len(sys.argv) != 5:
    raise SystemExit("usage: stage7_multiparticle_loaded_state_reference.py PROJECT.json STATE.vpsd OUTPUT.json ENGINE")

project_path, state_path, output_path, engine = sys.argv[1:]
main(project_path, output_path, engine, state_path)
