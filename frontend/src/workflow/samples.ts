/* Bundled starter projects for the Project Setup gallery. */
import exampleSamples from './exampleSamples.json';

export type ExampleSample = {
  id: string;
  label: string;
  meta: string;
  source: string;
  note: string;
  sample: Record<string, unknown>;
};

/** Nine bundled starters, including the accepted 22 nm FinFET bare-wafer demo. */
export const SAMPLE_EXAMPLES = exampleSamples as ExampleSample[];

export const SAMPLE_2D_PLANE: Record<string, unknown> = {
  "schemaVersion": 7,
  "name": "2D 平面刻蚀示例",
  "dimension": 2,
  "units": {
    "length": "um",
    "time": "s",
    "rate": "um/s",
    "angle": "deg"
  },
  "domain": {
    "gridDelta": 0.2,
    "xExtent": 4.0,
    "xBoundary": "reflective"
  },
  "geometry": {
    "kind": "plane",
    "height": 0.0,
    "substrateMaterial": "Si"
  },
  "steps": [
    {
      "id": "p3_step_01",
      "name": "etch",
      "enabled": true,
      "operation": "etch",
      "model": "isotropic",
      "rateMagnitude": 0.02,
      "duration": 0.02,
      "rateMode": "uniform"
    }
  ],
  "solver": {
    "timeStepRatio": 0.25,
    "raysPerPoint": 16,
    "cpuThreads": 1,
    "random": {
      "mode": "fixed",
      "seed": 7
    },
    "enginePreference": "cpu",
    "timeoutSeconds": 30
  },
  "view": {
    "panX": 0.0,
    "panH": 0.0,
    "zoom": 1.0,
    "showGrid": true
  }
};

export const SAMPLE_2D_TRENCH: Record<string, unknown> = {
  "schemaVersion": 7,
  "name": "2D Trench 示例",
  "dimension": 2,
  "units": {
    "length": "um",
    "time": "s",
    "rate": "um/s",
    "angle": "deg"
  },
  "domain": {
    "gridDelta": 0.1,
    "xExtent": 4.0,
    "xBoundary": "reflective"
  },
  "geometry": {
    "kind": "trench",
    "width": 1.0,
    "depth": 2.0,
    "taperAngle": 0.0,
    "maskHeight": 0.0,
    "maskTaperAngle": 0.0,
    "substrateMaterial": "Si",
    "maskMaterial": "Mask"
  },
  "steps": [
    {
      "id": "fixture_step_01",
      "name": "etch",
      "enabled": true,
      "operation": "etch",
      "model": "isotropic",
      "rateMagnitude": 0.02,
      "duration": 0.02,
      "rateMode": "uniform"
    }
  ],
  "solver": {
    "timeStepRatio": 0.25,
    "raysPerPoint": 32,
    "cpuThreads": 1,
    "random": {
      "mode": "fixed",
      "seed": 7
    },
    "enginePreference": "cpu",
    "timeoutSeconds": 30
  },
  "view": {
    "panX": 0.0,
    "panH": 0.0,
    "zoom": 1.0,
    "showGrid": true
  }
};

export const SAMPLE_3D_HOLE: Record<string, unknown> = {
  "schemaVersion": 7,
  "name": "3D 孔刻蚀示例",
  "dimension": 3,
  "units": {
    "length": "um",
    "time": "s",
    "rate": "um/s",
    "angle": "deg"
  },
  "domain": {
    "gridDelta": 0.2,
    "xExtent": 6.0,
    "yExtent": 4.0,
    "boundaryXY": "reflective"
  },
  "geometry": {
    "kind": "hole",
    "radius": 1.0,
    "depth": 2.0,
    "taperAngle": 0,
    "maskHeight": 0,
    "maskTaperAngle": 0,
    "substrateMaterial": "Si",
    "maskMaterial": "Mask"
  },
  "steps": [
    {
      "id": "step_demo_01",
      "name": "etch",
      "enabled": true,
      "operation": "etch",
      "model": "isotropic",
      "rateMagnitude": 0.02,
      "duration": 0.02,
      "rateMode": "uniform"
    }
  ],
  "solver": {
    "timeStepRatio": 0.25,
    "raysPerPoint": 16,
    "cpuThreads": 1,
    "random": {
      "mode": "fixed",
      "seed": 7
    },
    "enginePreference": "cpu",
    "timeoutSeconds": 60
  },
  "view3D": {
    "projection": "parallel",
    "camera": null,
    "displayMode": "surface",
    "visibleMaterialIds": [],
    "opacityByMaterial": {},
    "clip": {
      "enabled": false,
      "axis": "x",
      "position": 0.0,
      "keep": "positive"
    },
    "section": {
      "enabled": false,
      "axis": "y",
      "position": 0.0
    }
  }
};
