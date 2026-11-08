"""Exercise real native extraction with deliberately tiny application budgets."""
import sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'worker'))
import mesh3d,simulate
mode=os.environ.get('WORKBENCH_TEST_3D_LIMIT','points')
if mode=='points':mesh3d.LIMIT_POINTS=20
elif mode=='json':mesh3d.LIMIT_JSON=256
elif mode=='triangles':mesh3d.LIMIT_TRIANGLES=20
raise SystemExit(simulate.main())
