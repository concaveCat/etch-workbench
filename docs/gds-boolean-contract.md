# GDS 布尔几何合同（schemaVersion 7 / 3D GDS）

本合同描述 `gdsBoolean` 步骤的实际行为，全部基于安装版本 ViennaPS 4.6.2 + ViennaLS 5.8.5 的
真实调用与 `tests/test_gds_boolean.py` 的验收，不是从更新源码推断的能力。

## 支持范围

| 条件 | 要求 |
|---|---|
| `schemaVersion` | 7（1–6 不接受该模型，保持原样） |
| `dimension` | 3 |
| `geometry.kind` | `gds`（复用该项目已有的 GDS 资产，不引入新的上传路径） |
| 掩膜图层 | 所选 `structure` 闭包内、位于该图层的**闭合 BOUNDARY 多边形** |
| 掩膜高度 | `height >= 2*gridDelta` |
| 掩膜底面 | `baseHeight >= 0`（相对几何 z 框架；几何自身的 z 放置会叠加） |
| 坐标预算 | `|baseHeight + position[2]|/gridDelta <= 1e6`，且 `|position[i]|/gridDelta <= 1e6` |

二维项目、非 GDS 几何、选择不存在的图层/datatype，都会得到明确的 422（字段路径
`steps[i]`/`steps[i].gdsMask`），不会静默降级成盒体或空操作。

```jsonc
{
  "id": "step_contact_cut", "name": "接触孔", "enabled": true, "model": "gdsBoolean",
  "booleanOperation": "difference",           // difference | intersection | union
  "applyToAll": true,
  "gdsMask": {
    "layer": 2, "datatype": 0,                 // datatype 默认 0
    "unitScale": 1,                            // 与几何 unitScale 相乘
    "position": [0, 0, 0],                     // 相对版图放置；0 = 对齐
    "baseHeight": 0,                           // 掩膜底面高度（相对几何 z 框架）
    "height": 0.6,                             // 掩膜厚度
    "retainedRegion": "inside"                 // inside | outside
  }
}
```

## 坐标系与对齐

- GDS XY → 仿真 `x`/`y`，GDS 图层高度 → `z`，与 `geometry.kind=gds` 的映射一致。
- 步骤使用**几何放置 + 步骤偏移**：`unitScale = geometry.unitScale * gdsMask.unitScale`，
  XY 平移 = `geometry.position[:2] + gdsMask.position[:2]`，所以 `position=[0,0,0]`
  与版图图层严格对齐。
- `baseHeight` 相对几何 z 框架，实际底面 = `gdsMask.baseHeight + gdsMask.position[2] + geometry.position[2]`。
  例如只切上层薄膜：把 `baseHeight` 设为该薄膜底面高度、`height` 设为薄膜厚度。
- 实际生效的平移/缩放、`actualPositionUm`、`baseHeight`、`topHeight`、`filteredFile` 与构造统计
  都会写入该步的 `actualParameters` 和运行目录的 `gds-boolean-<stepId>.json`。

## 原生调用顺序

1. 校验运行目录内的资产副本 SHA-256 等于 `geometry.assetId`；不匹配直接失败。
2. `backend.gds.select_and_transform` 过滤到所选 `layer/datatype`，生成 `gds-boolean-<stepId>-selected.gds`。
   其余图形移到未使用的支撑图层，只为让原生读者得到库边界。
3. `viennaps.d2.GDSReader` + `GDSGeometry.layerToLevelSet(layer, True)` 读多边形内部；若原生帧
   缺陷导致空内部，用一格外扩帧重读（与几何层构建相同的恢复路径），仍为空则报错。
4. `worker.gds_mask.build_aligned_gds_mask` 把多边形竖直拉伸到 `[baseHeight, baseHeight+height]`、
   封顶封底、按 `retainedRegion` 取正向/反向掩膜，并重采样到仿真网格。
5. `Domain.applyBooleanOperation(mask, BooleanOperationEnum.<enum>, applyToAll)`；枚举映射
   `difference→RELATIVE_COMPLEMENT`、`intersection→INTERSECT`、`union→UNION`。
6. 布尔后检查没有任何 level set 被清空；出现空 level set 直接失败，不保存被破坏的结构。

## 语义与限制

- **只是几何**：操数是多边形竖直拉伸体（俯视形状精确、侧壁垂直、无锥角），没有光刻胶、
  光学邻近/偏置、曝光阈值、侧壁角、沉积/刻蚀选择比或保形覆盖。它是"图形转移"而不是
  "带掩膜的刻蚀"。
- `difference` 从所有（`applyToAll=true`）或仅顶层（`false`）材料界面减去掩膜体：可挖孔/开槽。
  `union` 把掩膜体并入界面：可在多边形区域补加顶层材料。`intersection` 只保留掩膜体内材料。
- `applyToAll=false` 只改顶层材料，下层可能高于新的顶层；只有明确需要时才关闭。
- `retainedRegion=outside` 是反向掩膜；配 `difference` 会移除掩膜外的几乎所有材料，
  配 `union` 会在掩膜外补加材料。界面会给出提示，但不会替你改写参数。
- 横向边界精度受网格限制：实测孔壁在 1–2 个 `gridDelta` 内与多边形边界一致
  （见 `tests/test_gds_boolean.py::test_difference_mask_cuts_the_expected_hole`），
  不承诺亚网格精度，也不做边界顶点投影。
- 空掩膜（例如多边形落在计算域外）会让运行**失败**并给出
  `GDS 掩膜在当前计算域内为空…`，不会静默变成空操作。
- `backend/preflight.py` 的 `GEOMETRY_ONLY_MODELS` 尚未列入 `gdsBoolean`，因此产物预估按
  通用步骤权重 1.0 计算（比几何步骤的 0.85 更保守），不影响几何或原生调用。该文件不在本
  任务改动范围内。

## 接触孔 / M0 图形化是否可用

- 可用：把接触孔/M0 的版图图层作为 `gdsBoolean` 操作数，在已有的 3D GDS 结构上挖孔、
  开槽或补加金属区域，得到真实的拓扑和材料分布，作为后续工艺步骤的起点。
- 仍需真实工艺模型的部分：光刻胶图形化、光学邻近/偏置与分辨率、刻蚀/沉积选择比、
  侧壁角与保形性、以及"掩膜在刻蚀中被消耗"的行为。`gdsBoolean` 不模拟这些；接触孔/M0
  的最终形貌仍需在其后接 selective 刻蚀/沉积（例如 `rateMode=material` 或专用模型）步骤。
