import type { View3D } from '../types3d';

// Hand-written shapes for the workbench HTTP API. They mirror the JSON the
// backend emits; keep them in sync with backend/ responses when fields change.

export type Units = { length: 'um' | 'nm'; time: 's'; rate: 'um/s' | 'nm/s'; angle: 'deg' };
export type Domain = { gridDelta: number; xExtent: number; xBoundary?: 'reflective' | 'periodic'; yExtent?: number; boundaryXY?: 'reflective' | 'periodic' };
export type Layer = { id: string; name: string; material: string; thickness: number; role: 'film' | 'mask'; source?: 'gds-mask' };
export type GDSMaskPattern = { name: string; assetId: string; fileName: string; structure: string; layer: number; datatype: number; unitScale: number; position: [number, number, number]; baseHeight: number; height: number; material: string; retainedRegion: 'inside' | 'outside'; protectsEtch: boolean };
export type StackGeometry = { kind: 'stack'; substrateMaterial: string; baseHeight: number; layers: Layer[]; pattern: { kind: 'none' } | { kind: 'trench'; width: number; taperAngle: number } | { kind: 'hole'; radius: number; taperAngle: number } | { kind: 'gds' }; gdsMask?: GDSMaskPattern };
export type HoleGeometry = { kind: 'hole'; radius: number; depth: number; taperAngle: number; maskHeight: number; maskTaperAngle: number; substrateMaterial: string; maskMaterial?: string };
export type PlaneGeometry = { kind: 'plane'; height: number; substrateMaterial: string };
export type TrenchGeometry = { kind: 'trench'; width: number; depth: number; taperAngle: number; maskHeight: number; maskTaperAngle: number; substrateMaterial: string; maskMaterial?: string };
export type FinGeometry = { kind: 'fin'; width: number; height: number; taperAngle: number; maskHeight: number; maskTaperAngle: number; substrateMaterial: string; maskMaterial: string; construction?: 'native' | 'boolean'; extrusion?: { extent: [number, number]; axis: 1 } };
export type GDSLayerMapping = { layer: number; material: string; baseHeight?: number; height?: number; mask?: boolean };
export type GDSGeometry = { kind: 'gds'; assetId: string; fileName: string; structure: string; unitScale: number; position: [number, number, number]; layers: GDSLayerMapping[] };
export type Geometry = PlaneGeometry | TrenchGeometry | HoleGeometry | StackGeometry | FinGeometry | GDSGeometry;
export type GDSAsset = { assetId: string; fileName: string; structures: { name: string; layers: number[]; layerDatatypes?: [number, number][]; maskLayerDatatypes?: [number, number][]; references: string[] }[]; units: { userUnitsPerDatabaseUnit: number; metersPerDatabaseUnit: number; micrometersPerNativeUserUnit: number }; fileSizeBytes: number };
export type IonSpecies = { kind: 'ion'; label: string; sourcePower: number; thetaRMin: number; thetaRMax: number; minAngle: number; B_sp: number; meanEnergy: number; sigmaEnergy: number; thresholdEnergy: number; inflectAngle: number; n: number };
export type NeutralSpecies = { kind: 'neutral'; label: string; defaultStickingProbability: number; stickingProbabilityByMaterial: Record<string, number> };
export type ParticleSpecies = IonSpecies | NeutralSpecies;
export type MultiParticleRateCoefficients = { defaultBySpecies: Record<string, number>; byMaterial: Record<string, Record<string, number>> };
export type SpecializedField = { key: string; label: string; kind: 'number' | 'boolean' | 'enum' | 'vector3' | 'material' | 'materialOptional' | 'text' | 'materialRates' | 'fluorocarbonMaterials'; default: unknown; minimum?: number; maximum?: number; step?: number; nativeUnit?: string | null; options?: { value: string; label: string }[]; defaults?: Record<string, number> };
export type SpecializedModel = { id: string; label: string; nativeClass: string; operation: 'etch' | 'deposit' | 'native'; usesFlux: boolean; dimensions?: number[]; depositMaterial?: string; depositMaterialSelectable?: boolean; fields: SpecializedField[] };
export type SpecializedCatalog = { schemaVersion: number; engineVersion: string; unitsNote: string; models: SpecializedModel[] };
export type CsvRateAsset = { assetId: string; fileName: string; dimension: 2 | 3; columns: string[]; rowCount: number; x: { min: number; max: number; count: number }; y?: { min: number; max: number; count: number }; rate: { min: number; max: number; sign: string }; linearCompatible: boolean; sizeBytes: number };
export type DirectionalMaterialRate = { directional: number; isotropic: number };
export type Step = {
  id: string; name: string; enabled: boolean; operation?: 'etch' | 'deposit' | 'native'; model: 'isotropic' | 'singleParticle' | 'multiParticle' | 'directional' | 'specialized' | 'csvProcess' | 'removeMaterial' | 'planarize' | 'boolean' | 'gdsBoolean';
  rateMagnitude?: number; directionalRateMagnitude?: number; isotropicRateMagnitude?: number; direction?: [number, number, number]; calculateVisibility?: boolean;
  isotropicMethod?: 'auto' | 'levelSet' | 'geometric'; isotropicTimeStepRatio?: number;
  duration?: number; depositMaterial?: string; stickingProbability?: number; sourceExponent?: number;
  // `materialRates` is shared by two schemas: isotropic steps store a scalar rate per
  // material, directional steps store a {directional, isotropic} pair per material.
  rateMode?: 'uniform' | 'material'; materialRates?: Record<string, number> | Record<string, DirectionalMaterialRate>; defaultMaterialRate?: DirectionalMaterialRate; selectivityReference?: string;
  species?: ParticleSpecies[]; rateCoefficients?: MultiParticleRateCoefficients;
  nativeModel?: string; nativeParameters?: Record<string, unknown>;
  assetId?: string; fileName?: string; offset?: [number, number]; isotropicComponent?: number; directionalComponent?: number; interpolationMode?: 'LINEAR' | 'IDW'; idwNeighbors?: number;
  material?: string; cutoffHeight?: number; stopMaterial?: string; overPolish?: number; booleanOperation?: 'union' | 'intersection' | 'difference'; position?: [number, number, number]; width?: number; height?: number; depth?: number; angle?: number; applyToAll?: boolean;
  gdsMask?: { layer: number; datatype: number; unitScale: number; position: [number, number, number]; baseHeight: number; height: number; retainedRegion: 'inside' | 'outside' };
};
export type FixedRandom = { mode: 'fixed'; seed: number };
export type RandomPolicy = FixedRandom | { mode: 'random' };
export type AdvectionSettings = { spatialScheme: string; temporalScheme: string; adaptiveTimeStepping: boolean; adaptiveTimeStepSubdivisions: number; calculateIntermediateVelocities: boolean; checkDissipation: boolean; dissipationAlpha: number; ignoreVoids: boolean; velocityOutput: boolean };
export type RayTracingSettings = { diskRadius: number; ignoreFluxBoundaries: boolean; maxBoundaryHits: number; maxReflections: number; minNodeDistanceFactor: number; normalizationType: 'SOURCE' | 'MAX'; smoothingNeighbors: number };
export type Solver = { timeStepRatio: number; raysPerPoint: number; cpuThreads: number; random: RandomPolicy; enginePreference: 'auto' | 'cpu' | 'gpu'; gpuDeviceId?: string; engineId?: string; engineKernel?: string; engineBackend?: 'cpu' | 'gpu'; timeoutSeconds: number; advection?: AdvectionSettings; rayTracing?: RayTracingSettings };
export type ViewSettings = { panX: number; panH: number; zoom: number; showGrid: boolean };
export type DomainStateRef = { formatVersion: 1; stateId: string; structureHash: string; dimension: 2 | 3; gridDelta: number; xExtent: number; yExtent?: number; boundary: 'reflective' | 'periodic'; boundaryConditions: string[]; materialIdsByLevelSetIndex: number[]; levelSetCount: number; viennapsVersion: string; viennalsVersion: string; sourceRunId?: string; sourceStepId?: string };
export type OutputSettings = { levelSets: boolean; levelSetMeshes: boolean; surfaceMeshVtk: boolean; volumeMesh: boolean; nativeMetadata: boolean };
export type MaterialDefinition = { name: string; id: number; customId: number; color: string; category?: string; densityGcm3?: number; conductive: boolean; description: string };
export type Project = { schemaVersion: 1 | 2 | 3 | 4 | 5 | 6 | 7; name: string; dimension: 2 | 3; units: Units; domain: Domain; geometry: Geometry; steps: Step[]; solver: Solver; view?: ViewSettings; view3D?: View3D; continuationState?: DomainStateRef; materialDefinitions?: MaterialDefinition[]; outputSettings?: OutputSettings };
export type NativeArtifact = { kind: string; fileName: string; url: string; format: string; sizeBytes: number; sha256: string; levelSetIndex?: number; materialId?: number; association?: string; nativeUnit?: string | null; metadataLevel?: string };
export type Snapshot = {
  schemaVersion: number; dimension?: number; surfaceBounds?: number[]; substrateMaterialId?: number; runId: string; stepId: string; stepName: string; stepIndex: number; axisMap: { x: number; h: number; z: number };
  coordinateUnit: string; points: number[][]; lines?: number[][]; triangles?: number[][]; cellData: { materialIds: number[] };
  materials: Record<string, { name: string; color?: string; levelSetIndexes: number[]; sourceStepId: string | null }>;
  geometryReadback?: Record<string, unknown>;
  outputs?: NativeArtifact[];
  sliceReadback?: { nativeOperation: string; axis: number; axisLabel: string; position: number; sourceDimension: number; outputDimension: number; viewerClipping: boolean; levelSetCount: number; materialIdsByLevelSetIndex: number[] };
  mesh: { vtk?: string; pointCount: number; lineCount?: number; triangleCount?: number }; gridDelta: number; materialIdsByLevelSetIndex: number[]; noOp: boolean;
  materialTops?: Record<string, { topHeight: number; medianTopHeight: number; upFacingArea: number; upFacingCellCount: number }>;
  measuredRemoval?: MeasuredChange;
  measuredAdded?: MeasuredChange;
};
export type MeasuredChangeRow = { material: string; topHeightBefore?: number | null; topHeightAfter?: number | null; medianHeightBefore?: number | null; medianHeightAfter?: number | null; topHeightDelta?: number | null; medianHeightDelta?: number | null; removedThickness?: number | null; addedThickness?: number | null; removedCompletely?: boolean };
export type MeasuredChange = MeasuredChangeRow & { remainingTopHeight?: number | null; thicknessBasis?: string | null; materialAppeared?: boolean | null; method?: string | null; materials?: MeasuredChangeRow[] };
export type SnapshotRow = { stepId: string; name: string; stepIndex: number; snapshot: string; vtk: string; pointCount: number; lineCount?: number; triangleCount?: number; materialIds: number[]; noOp?: boolean; geometryReadback?: Record<string, unknown>; sliceReadback?: Snapshot['sliceReadback']; nativeState?: DomainStateRef & { fileName: string } };
export type RunSummary = { runId: string; kind: string; status: string; createdAt?: string; finishedAt?: string; requestedInputHash?: string; failureReason?: string };
export type RunTermination = { reason?: string | null; returnCode?: number | null; rssLimitMiB?: number | null; peakObservedRssBytes?: number | null };
export type IntermediateOutputRef = { stepId: string; fileName: string; url: string; format: string; pointCount: number | null; cellCounts: Record<string, number>; coordinateUnit: string | null; axisMap: Record<string, number> | null; fields: { name: string; association: 'pointData' | 'cellData'; components: number; tuples: number; nativeType?: string | null; nativeUnit: string | null; unitStatus?: string }[]; sizeBytes: number; sha256: string; associationPolicy?: string };
export type V2StepRecord = {
  stepId: string;
  plan: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  state: Record<string, unknown> | null;
};
export type RunInfo = {
  runId: string; kind: string; status: string; currentStepId: string | null; completedStepIds: string[]; startedAt?: string | null;
  elapsedSeconds: number; requestedInputHash: string; executionHash?: string; failureReason?: string | null; termination?: RunTermination | null; stepTotal?: number;
  runMode?: 'from-initial' | 'from-domain-state'; initialState?: DomainStateRef | null;
  log: { cursor: number; nextCursor: number; truncated: boolean; text: string }; snapshots: SnapshotRow[];
  actualExecution?: { stepId: string; model?: string; operation?: string; noOp?: boolean; actualEngine?: string; actualSeed?: number | null; actualRate?: number | null; actualParameters?: Record<string, unknown> }[];
  fluxResult?: { fileName: string; vtk: string; meshIdentity: string; pointCount: number; fieldCount: number; fieldNames: string[]; actualEngine: string; actualSeed: number };
  intermediateOutputs?: IntermediateOutputRef[];
  v2Steps?: V2StepRecord[];
};
export type FluxField = { name: string; association: 'pointData' | 'cellData'; components: 1 | 3; tuples: number; nativeUnit: string | null; unitStatus: string };
export type FluxResult = {
  schemaVersion: 1; source: string; dimension: 2 | 3; axisMap: Record<string, number>; coordinateUnit: string; fieldUnitPolicy: string;
  meshIdentity: string; mesh: { pointCount: number; cellCounts: Record<string, number>; vtk: string; format: string };
  points: number[][]; topology: Record<string, number[][]>; pointData: { scalars: Record<string, number[]>; vectors: Record<string, number[][]> };
  cellData: { scalars: Record<string, number[]>; vectors: Record<string, number[][]> }; fields: FluxField[];
  execution: { stepId: string; stepName: string; model: string; operation: string; actualRate: number | null; actualEngine: string; actualSeed: number; cpuThreads: number; rays: Record<string, unknown>; elapsedSeconds: number; processDuration: string; nativeModel?: Record<string, unknown> };
  versions: { viennaps: string; viennals: string };
};
export type ProjectRow = { id: string; name: string; updatedAt: string };
export type GpuMaterialScope = { kind: string; engineVersion: string; builtInLegacyIds: number[]; customLegacyIdRange: [number, number]; boundaries: string[]; roleKindsByGeometry: Record<string, string[][]>; bindingKeysByOperation: Record<string, string[]>; evidence: string[] };
export type GpuSpeciesScope = { kind: string; evidence: string[] };
export type GpuKnownFailure = { model: string; dimension: number; operation: string; boundaryXY: string; geometryKind: string; engineVersion: string; failure: string; engines?: string[]; testedProfiles?: string[]; evidence: string[] };
export type GpuEngineLimitation = { model: string; engine: string; engineVersion: string; limit: string; maxIonSpecies: number; dimensions: number[]; extraIonBehavior: string; cpuVerifiedProductDirectCases: number; evidence: string[] };
export type GpuCapabilityRow = { status: string; schemaVersions?: number[]; dimension: number; model: string; nativeModel?: string; operation: string; boundaryXY: string; engine: string; geometryKinds?: string[]; patternKinds?: string[]; finExtrusionMode?: 'direct' | 'extruded'; finConstruction?: 'native' | 'boolean'; materials?: string[]; materialRoles?: Record<string, string[]>; materialBindings?: Record<string, string>; speciesSignature?: { species: ParticleSpecies[]; rateCoefficients: MultiParticleRateCoefficients }; executionStrategy?: string; solverProfile?: { gridDelta: number; timeStepRatio: number; raysPerPoint: number; randomMode: 'fixed' | 'random'; advection?: AdvectionSettings; rayTracing?: RayTracingSettings }; materialScope?: GpuMaterialScope; speciesScope?: GpuSpeciesScope; gdsScope?: { kind: string; engineVersion: string; dimensions: number[]; maxLayerMappings: number; supports3DMask: boolean } };
export type GpuProfileFailure = { dimension: number; model: string; operation: string; boundaryXY: string; engine: string; geometryKind: string; solverProfile: Record<string, unknown>; materials: string[]; materialRoles: Record<string, string[]>; materialBindings: Record<string, string>; speciesSignature?: { species: ParticleSpecies[]; rateCoefficients: MultiParticleRateCoefficients } | null; reason: string };
export type ArtifactPreflightPolicy = { capBytes?: number | null; capMiB?: number | null; artifactLimitSource?: string | null };
export type RunPolicy = {
  maximumSteps?: number | null; maximumArtifactBytes?: number | null; maximumResultArchiveBytes?: number | null; maximumArtifactMiB?: number | null;
  artifactLimitBytes?: number | null; artifactLimitMiB?: number | null; artifactLimitSource?: string | null; artifactLimitEnvironmentVariable?: string | null;
  artifactLimitDefaultMiB?: number | null; artifactLimitMinimumMiB?: number | null; artifactLimitMaximumMiB?: number | null;
  artifactPreflight?: ArtifactPreflightPolicy; cpuThreads?: { default?: number; minimum?: number; maximum?: number; limitSource?: string };
};
export type Capabilities = { capabilities?: { gpuModelRows?: GpuCapabilityRow[]; knownGpuProfileFailures?: GpuProfileFailure[]; knownNativeFailures?: GpuKnownFailure[]; knownEngineLimitations?: GpuEngineLimitation[]; threeDimensional?: { rows?: GpuCapabilityRow[] }; engines?: { gpu?: { status?: string; exposed?: boolean; engine?: string } } }; resourcePolicy?: { run?: RunPolicy } };
export type GpuDevice = { index: number; name: string; pciBusId: string; uuid: string; memoryTotalMiB: number | null; vendor: string; computeApi: string };
export type GpuDeviceInventory = { status: 'available' | 'unavailable' | 'error'; devices: GpuDevice[]; preferredGpuDeviceId: string | null; reason: string | null; probe?: { bounded: boolean; returnCode?: number; elapsedSeconds?: number } };
export type EngineCapability = { engineId: string; version: string; kind: string; backends: string[]; capabilities: string[]; kernels: string[]; devices: string[] };
export type EngineCatalog = { engines: EngineCapability[] };

// Material catalog entry from GET /api/materials (built-in and custom).
export type MaterialCatalogItem = { name: string; id: number; color: string; isCustom?: boolean; category?: string; densityGcm3?: number; conductive?: boolean; description?: string; customId?: number };
