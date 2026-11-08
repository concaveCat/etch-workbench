"""Native map-constructor reference independent of the worker implementation."""
import json,sys
from pathlib import Path
import viennaps as ps
from materials_direct_reference import build,capture

def run(project,output):
    domain,protected=build(project);dim=project['dimension'];reg=ps.MaterialRegistry.instance();results={'initial':capture(domain,dim)}
    for step in project['steps']:
        if not step['enabled']:continue
        profile=step.get('rateMode','uniform')=='material'
        default=step['rateMagnitude'];table=step.get('materialRates',{}) if profile else {}
        moving=default>0 or any(v>0 for v in table.values())
        if moving:
            signed=default*(1 if step['operation']=='deposit' else -1)
            if step['operation']=='deposit':domain.duplicateTopLevelSet(reg.getMaterial(step['depositMaterial']))
            if profile:
                rates={reg.getMaterial(name):-value for name,value in table.items()}
                if step['model']=='isotropic':model=ps.IsotropicProcess(materialRates=rates,defaultRate=signed)
                else:
                    model=ps.SingleParticleProcess(materialRates=rates,stickingProbability=step['stickingProbability'],sourceExponent=step['sourceExponent'])
                    model.setDefaultRate(signed)
            elif step['model']=='isotropic':model=ps.IsotropicProcess(rate=signed,maskMaterials=protected)
            else:model=ps.SingleParticleProcess(rate=signed,stickingProbability=step['stickingProbability'],sourceExponent=step['sourceExponent'],maskMaterials=protected)
            process=ps.Process(domain,model,step['duration']);process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
            adv=ps.AdvectionParameters();adv.timeStepRatio=project['solver']['timeStepRatio'];process.setParameters(adv)
            if step['model']=='singleParticle':
                ray=ps.RayTracingParameters();ray.raysPerPoint=project['solver']['raysPerPoint'];ray.rngSeed=project['solver']['random']['seed'];ray.useRandomSeeds=False;process.setParameters(ray)
            process.apply()
        results[step['id']]=capture(domain,dim)
    Path(output).write_text(json.dumps(results))

if __name__=='__main__':run(json.loads(Path(sys.argv[1]).read_text()),sys.argv[2])
