import json
from pathlib import Path
import viennaps as ps
ps.setDimension(3)
try:
 available=ps.gpuAvailable()
 print(json.dumps({'dimension':3,'gpuAvailable':available,'exposed':False,'reason':'availability alone does not verify a combination'}))
except Exception as e:
 print(json.dumps({'dimension':3,'gpuAvailable':False,'exposed':False,'reason':str(e)}))
