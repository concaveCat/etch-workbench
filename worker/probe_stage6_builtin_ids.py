"""Check ViennaPS built-in numeric IDs for the custom-ID namespace; bounded_child only."""
import json, viennaps as ps
reg=ps.MaterialRegistry.instance(); rows=[]
for name,value in vars(ps.Material).items():
 try:
  if isinstance(value,ps.Material): rows.append({'name':reg.getName(value),'legacyId':int(value.legacyId()),'builtIn':bool(value.isBuiltIn())})
 except Exception: pass
rows=sorted({row['name']:row for row in rows}.values(),key=lambda row:(row['legacyId'],row['name']))
custom=reg.registerMaterial('Stage6 Namespace Probe')
print(json.dumps({'builtInCount':len(rows),'maxBuiltInLegacyId':max((r['legacyId'] for r in rows),default=-1),'builtIns':rows,'custom':{'id':custom.legacyId(),'customId':custom.customId()}},ensure_ascii=False),flush=True)
