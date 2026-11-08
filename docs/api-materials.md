# Material catalog API

`GET /api/materials` returns the locally verified ViennaPS 4.6.2 material registry as
`{schemaVersion:1, engineVersion:"4.6.2", materials:[{name,id,color}]}`. The served
catalog is [materials-catalog.json](materials-catalog.json); IDs and colors are read
from the installed native `MaterialRegistry` during the bounded probe and are not
invented by the web process. The worker resolves a selected name back through the
native registry before constructing geometry or a deposit.

Project schema versions 1 and 2 keep their original fixed-material request shapes.
Schema version 3 accepts catalog material names for substrate, mask, stack layers,
and deposition. A material has a physical native ID; its `film` or `mask` role is
separate editor metadata. A single native material ID cannot be both protected and
unprotected in one project. The installed IsotropicProcess and SingleParticleProcess
bindings were verified with multiple protected materials through maskMaterials.
Nonzero deposition of a protected material is rejected in this milestone.

Changing material does not change the process rate or sticking probability. These
remain global model settings in this release.

V3 stack layers are ordered bottom to top. The optional trench or hole stencil
cuts only the top mask. Patterns use epsilon=gridDelta*1e-3, bottom-epsilon as the
stencil position, thickness+2*epsilon as its height, and -taperAngle. The raw
simulation Domain supplies V3 snapshots and VTK; unlike legacy display exports,
same-material internal interfaces are not merged or reconstructed.

V3 layer names/IDs and camera settings are excluded from the physical input hash.
Thickness, order, material, role, and pattern are included. New material/stack
particle GPU combinations are disabled pending a separate verification. The
existing legacy projects and their verified engines retain their contracts.

V3 visibleMaterialIds is an explicit list: empty hides all materials. New 3D
projects initialize this list from the verified catalog. Legacy empty-list
display semantics remain unchanged.
