"""Exercise the application's bounded GPU inventory implementation."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.gpu_devices import cuda_devices


if __name__ == "__main__":
    print(json.dumps(cuda_devices(refresh=True), ensure_ascii=False, indent=2, sort_keys=True))
