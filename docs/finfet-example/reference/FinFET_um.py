"""Micrometre-frame native twin of ``FinFET.py``.

``FinFET.py`` is authored in nanometres. The workbench project uses
micrometres, so every length, radius, half-axis, cutoff and rate in this file
is the original value times exactly 0.001; durations are unchanged. The
operation order and material sequence are identical.

This file exists because the installed ViennaPS geometric-strategy mask
construction is not exactly scale invariant: with the same operation order,
the nanometre run and this micrometre run agree on surfaces 0-13 and then take
different boolean branches while building the dummy-gate mask (surface 14).
The acceptance test compares the workbench (which must run in its declared
micrometre frame) with this file's checkpoints. ``expected.json`` from the
unmodified nanometre script remains the source evidence; ``expected-um.json``
is the project-frame reference.

The ``maxDissipation`` value stays 100 because the workbench project passes the
same value; ``rate111``/``rate100`` stay at the native defaults 0.5/1.0 because
they are orientation multipliers, not a material rate.
"""

import viennaps as ps

ps.setDimension(3)
surfaceNum = 0


def writeSurface(domain):
    global surfaceNum
    domain.saveSurfaceMesh("FinFET_" + str(surfaceNum) + ".vtp", True)
    surfaceNum += 1


ps.Logger.setLogLevel(ps.LogLevel.ERROR)

boundaryConds = [
    ps.BoundaryType.REFLECTIVE_BOUNDARY,
    ps.BoundaryType.REFLECTIVE_BOUNDARY,
    ps.BoundaryType.INFINITE_BOUNDARY,
]
bounds = [0.0, 0.09, 0.0, 0.10, 0.0, 0.07]  # in micrometres
gridDelta = 0.00079

domain = ps.Domain(bounds, boundaryConds, gridDelta)

ps.MakePlane(domain, 0.07, ps.Material.Si).apply()
writeSurface(domain)

# Add double patterning mask
box = ps.LevelSet(domain.getGrid())
minPoints = [0.030, -0.010, 0.0699]
maxPoints = [0.060, 0.110, 0.090]
geo = ps.MakeGeometry(box, ps.Box(minPoints, maxPoints)).apply()
domain.insertNextLevelSetAsMaterial(box, ps.Material.Mask)
writeSurface(domain)

# Double patterning processes
print("Double patterning processes ...", end="", flush=True)
thickness = 0.004  # um
domain.duplicateTopLevelSet(ps.Material.Metal)
growth = ps.SphereDistribution(radius=thickness)
ps.Process(domain, growth, 0).apply()
print(" done")
writeSurface(domain)

# DP-Patterning
print("DP-Patterning ...", end="", flush=True)
etchDepth = 0.006  # um
dist = ps.BoxDistribution(halfAxes=[-gridDelta, -gridDelta, -etchDepth])
dist.addMaskMaterial(ps.Material.Si)
ps.Process(domain, dist).apply()
print(" done")
writeSurface(domain)

# Remove mask with boolean operation
domain.removeMaterial(ps.Material.Mask)
writeSurface(domain)

# pattern si
print("Si-Patterning ...", end="", flush=True)
etchDepth = 0.090  # um
model = ps.DirectionalProcess(
    direction=[0.0, 0.0, -1.0],
    materialRates={ps.Material.Si: [0.001, 0.0001]},
)
ps.Process(domain, model, 90.0).apply()
print(" done")
writeSurface(domain)

# Remove DP mask (metal)
domain.removeTopLevelSet()
writeSurface(domain)

# deposit STI material
print("STI Deposition ...", end="", flush=True)
thickness = 0.035  # um
domain.duplicateTopLevelSet(ps.Material.SiO2)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# CMP at 0.08
ps.Planarize(domain, 0.080).apply()
writeSurface(domain)

# pattern STI material
print("STI Patterning ...", end="", flush=True)
dist = ps.SphereDistribution(radius=-0.035)
dist.addMaskMaterial(ps.Material.Si)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# deposit gate material
print("Gate Deposition HfO2 ...", end="", flush=True)
thickness = 0.002  # um
domain.duplicateTopLevelSet(ps.Material.HfO2)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

print("Gate Deposition PolySi ...", end="", flush=True)
thickness = 0.104  # um
domain.duplicateTopLevelSet(ps.Material.PolySi)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# CMP at 0.15
ps.Planarize(domain, 0.150).apply()
writeSurface(domain)

# dummy gate mask addition
box = ps.LevelSet(domain.getGrid())
minPoint = [-0.010, 0.030, 0.145]
maxPoint = [0.100, 0.070, 0.175]
geo = ps.MakeGeometry(box, ps.Box(minPoint, maxPoint)).apply()
domain.insertNextLevelSetAsMaterial(box, ps.Material.Mask)
writeSurface(domain)

# gate patterning
print("Dummy Gate Patterning ...", end="", flush=True)
masks = [ps.Material.Mask, ps.Material.Si, ps.Material.SiO2]
model = ps.BoxDistribution(halfAxes=[-gridDelta, -gridDelta, -0.110])
for mask in masks:
    model.addMaskMaterial(mask)
ps.Process(domain, model, 110.0).apply()
print(" done")
writeSurface(domain)

# Remove mask
domain.removeTopLevelSet()
writeSurface(domain)

# Spacer Deposition and Etch
print("Spacer Deposition and Etch ...", end="", flush=True)
thickness = 0.010  # um
domain.duplicateTopLevelSet(ps.Material.Si3N4)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# Spacer Etch
print("Spacer Etch ...", end="", flush=True)
masks = domain.getMaterialsInDomain()
masks.remove(ps.Material.Si3N4)
dist = ps.BoxDistribution(halfAxes=[-gridDelta, -gridDelta, -0.050])
for material in masks:
    dist.addMaskMaterial(material)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# isotropic etch (fin-release)
print("Fin-Release ...", end="", flush=True)
model = ps.IsotropicProcess(rate=0.0)
model.setMaterialRate(ps.Material.Si, -0.001)
advParams = ps.AdvectionParameters()
advParams.spatialScheme = ps.SpatialScheme.LAX_FRIEDRICHS_2ND_ORDER
process = ps.Process(domain, model, 5.0)
process.setParameters(advParams)
process.apply()
print(" done")
writeSurface(domain)

# source/drain epitaxy
print("S/D Epitaxy ...", end="", flush=True)
domain.duplicateTopLevelSet(ps.Material.SiGe)
advectionParams = ps.AdvectionParameters()
advectionParams.spatialScheme = ps.SpatialScheme.STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER
ps.StencilLocalLaxFriedrichsScalar.setMaxDissipation(100)
model = ps.SelectiveEpitaxy()
model.setMaterialRate(ps.Material.SiGe, 0.002)
model.setMaterialRate(ps.Material.Si, 0.002)
process = ps.Process(domain, model, 14.0)
process.setParameters(advectionParams)
process.apply()
print(" done")
writeSurface(domain)

# deposit dielectric
print("Dielectric Deposition ...", end="", flush=True)
thickness = 0.050  # um
domain.duplicateTopLevelSet(ps.Material.Dielectric)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# CMP at 0.09
ps.Planarize(domain, 0.090).apply()
writeSurface(domain)

# now remove gate and add new gate materials
domain.removeMaterial(ps.Material.PolySi)
writeSurface(domain)

# now deposit TiN and PolySi as replacement gate
print("Gate Deposition TiN ...", end="", flush=True)
thickness = 0.004  # um
domain.duplicateTopLevelSet(ps.Material.TiN)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

print("Gate Deposition PolySi ...", end="", flush=True)
thickness = 0.040  # um
domain.duplicateTopLevelSet(ps.Material.PolySi)
dist = ps.SphereDistribution(radius=thickness)
ps.Process(domain, dist, 0).apply()
print(" done")
writeSurface(domain)

# CMP at 0.09
ps.Planarize(domain, 0.090).apply()
writeSurface(domain)

domain.saveVolumeMesh("FinFET_Final", 0.05)
