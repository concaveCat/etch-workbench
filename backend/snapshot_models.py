"""Version-two output contract, also used by independent acceptance checks."""
from typing import Annotated,Literal
from pydantic import Field,model_validator
from .models import StrictModel,Number,Integer,Vector3

class Axes(StrictModel):
    x:Literal[0]
    y:Literal[1]
    z:Literal[2]

class CellMaterials(StrictModel):
    materialIds:list[Integer]

class Mesh3D(StrictModel):
    vtk:str|None=None
    pointCount:Annotated[Integer,Field(ge=0)]
    triangleCount:Annotated[Integer,Field(ge=0)]
    topology:Literal['triangles']

class Material(StrictModel):
    name:Literal['Mask','Si','SiO2']
    levelSetIndexes:list[Integer]
    sourceStepId:str|None

class MaterialTop(StrictModel):
    """Exposed top of one material, measured on the snapshot surface mesh."""
    topHeight:Number
    medianTopHeight:Number
    p90TopHeight:Number|None=None
    upFacingArea:Annotated[Number,Field(ge=0)]
    upFacingCellCount:Annotated[Integer,Field(ge=0)]

class MeasuredMaterialChange(StrictModel):
    """Thickness a step really added or removed, from the surface mesh only."""
    material:str
    topHeightBefore:Number|None=None
    topHeightAfter:Number|None=None
    medianHeightBefore:Number|None=None
    medianHeightAfter:Number|None=None
    topHeightDelta:Number|None=None
    medianHeightDelta:Number|None=None
    removedThickness:Number|None=None
    addedThickness:Number|None=None
    thicknessBasis:str|None=None
    removedCompletely:bool=False
    remainingTopHeight:Number|None=None
    materialAppeared:bool|None=None
    method:str|None=None
    materials:list[dict]|None=None

class Snapshot3D(StrictModel):
    schemaVersion:Literal[2]
    dimension:Literal[3]
    topology:Literal['triangles']
    runId:str
    stepId:str
    stepName:str
    stepIndex:Integer
    axisMap:Axes
    coordinateUnit:Literal['um','nm']
    points:list[Vector3]
    triangles:list[Annotated[list[Integer],Field(min_length=3,max_length=3)]]
    cellData:CellMaterials
    materials:dict[str,Material]
    mesh:Mesh3D
    gridDelta:Annotated[Number,Field(gt=0)]
    materialIdsByLevelSetIndex:list[Integer]
    sameMaterialInternalInterfacesHidden:bool
    noOp:bool
    measuredGeometry:dict[str,Number]|None
    surfaceBounds:Annotated[list[Number],Field(min_length=6,max_length=6)]
    domainReadback:dict
    materialTops:dict[str,MaterialTop]|None=None
    measuredRemoval:MeasuredMaterialChange|None=None
    measuredAdded:MeasuredMaterialChange|None=None
    substrateMaterialId:Integer|None=None
    materialSurfaceStatus:Literal['closed','unavailable']|None=None
    materialSurfaceError:str|None=None

    @model_validator(mode='after')
    def mesh_contract(self):
        if self.mesh.pointCount!=len(self.points) or self.mesh.triangleCount!=len(self.triangles) or len(self.cellData.materialIds)!=len(self.triangles):raise ValueError('mesh counts or triangle cell-material association mismatch')
        if any(len(set(t))!=3 or any(i<0 or i>=len(self.points) for i in t) for t in self.triangles):raise ValueError('invalid triangle connectivity')
        if any(str(m) not in self.materials for m in self.cellData.materialIds):raise ValueError('missing material mapping')
        return self
