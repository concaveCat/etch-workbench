# 材质感知平坦化（CMP）与实测厚度记录

本契约覆盖两个能力，均以安装版 ViennaPS 4.6.2 的实测行为为准：

- `planarize` 步骤可按材料停止（`stopMaterial` + `overPolish`），不再需要手工试出一个截断高度；
- 每个真正执行过的步骤记录各材料顶面的实测变化（`measuredRemoval` / `measuredAdded`）。

## 1. 原生能力边界（先说清限制）

安装版 `viennaps.Planarize(domain, cutoffHeight)` 只有一个高度参数：

```
__init__(self: viennaps.d2.Planarize, geometry: viennaps.d2.Domain, cutoffHeight: SupportsFloat = 0.0)
```

它把所有 level set 在 `cutoffHeight` 以上部分切掉，既没有“停在某种材料”的语义，也没有横向选择比。
ViennaLS 侧同样没有可对单个 level set 做 planarize 的绑定。因此本工作台**不伪造** CMP 选择性：

- `stopMaterial` 停高是**推导**出来的，不是原生选项：运行时先测量该材料当前暴露的顶面，
  取**面积加权 90 分位（p90）**高度，再减去 `overPolish`，把结果作为原生 `Planarize` 的高度参数。
- 用 p90 而不是中位数，是因为停止材料的暴露面常常是双峰的：22 nm FinFET 的 `ff22_w_fill` 快照里
  STI 平台在 0.036（占 SiO2 上表面面积约 54%），ILD 平台在 0.095（约 46%），中位数落在 0.037，
  会把整个栅极叠层切到场区以下（真实故障 run `f6f5481fd1be123be6f90fa6`：cutoff 0.034）。
  p90 选中 0.095，配合 `overPolish 0.003` 得到 0.092，与手工绕过的值一致而不再牺牲沟槽 W。
- 代价是阈值语义：只有当上方平台占该材料上表面面积 **10% 以上**时 p90 才会选它；低于 10% 的
  窄高毛刺不会改变切割高度。
- 落地后仍然是一刀切的全局高度切割；`stopMaterial` 自身高于该高度的部分也会被切掉。
  若停止材料真的多级且没有占主导的上方平台，推导出的单一高度会过切或欠切。
- 上述限制在 `/api/projects/validate` 的 `warnings` 里明确返回，步骤编辑器也会显示说明。

对 22 nm FinFET 流程（`/root/spx_examples/finfet22/workbench/README.md`，run
`f285e7d1146a6b526b2f63da` / `92f9877d838af76e314eda6e`）这正是需要的行为：ILD 顶面是平坦的，
停在 `SiO2` 就能一次去掉场区的 W/HfO2/TiN 覆盖层，而不会像“cutoff 0.092”那样把沟槽里的 W 一起削掉 3 nm。

## 2. 步骤字段

```json
{ "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.0 }
```

| 字段 | 规则 |
|---|---|
| `cutoffHeight` | 与 `stopMaterial` **二选一**（模型校验强制）；旧项目只写这个字段，行为与请求哈希都不变。 |
| `stopMaterial` | 材料目录中的名字，且在该步运行时必须已经存在（初始几何或更早的启用沉积）。 |
| `overPolish` | 仅用于 `stopMaterial`，单位 µm，`0 ≤ overPolish ≤ 100`；缺省等价于 0。 |

有效截断高度 = `measuredStopTopHeight - overPolish`，其中 `measuredStopTopHeight` 是该材料
上表面单元的**面积加权 90 分位高度**（`p90TopHeight`；不是最高点，也不是中位数：
一条窄而高的毛刺不应改变整片切割高度，而更大的下方平台也不应把切割拉到下方）。

该步的 `actualParameters` 记录：

```json
{ "geometryOperation": "planarize", "cutoffMode": "material-aware",
  "stopMaterial": "SiO2", "stopMaterialId": 30,
  "measuredStopTopHeight": 0.095, "stopHeight": 0.095,
  "stopHeightMethod": "area-weighted-p90", "stopHeightPercentile": 0.9,
  "stopMaterialMedianTopHeight": 0.037, "stopMaterialHighestTopHeight": 0.095,
  "stopMaterialUpFacingArea": 0.4347,
  "overPolish": 0.003, "cutoffHeight": 0.092,
  "cutoffDerivation": "area-weighted-p90 height of the up-facing stopMaterial surface ... minus overPolish",
  "nativePlanarizeScope": "viennaps.Planarize(domain, cutoffHeight) cuts every level set above the derived height; lateral selectivity is not modeled" }
```

运行期若该材料在步开始时没有上表面单元（被完全覆盖或已移除），步骤以明确错误失败，提示改用
`cutoffHeight` 或先沉积/暴露该材料；不会静默退回到某个默认高度。

## 3. 实测厚度记录

表面网格在每一步本来就会提取一次（用于快照），因此测量没有额外原生开销，也**不生成体网格**。
每步把上表面单元（法向与竖直方向夹角 ≤ 60°）按材料聚合：

- `topHeight`：该材料上表面最高点（仅作参考）
- `medianTopHeight`：面积加权中位高度（用于实测厚度）
- `p90TopHeight`：面积加权 90 分位高度（用于 CMP 停高）
- `upFacingArea`、`upFacingCellCount`：证据量

CMP 停高与“本步去/加了多少”用**两个不同**的统计量，这是有意为之：停高要能跳过更大的下方平台，
所以用 p90；厚度要描述“典型暴露面移动了多少”，所以用中位数。二者混用会各自出错
（中位停高把 22 nm FinFET 的 W CMP 切到 0.034；最高点差值把 SiN 侧墙回刻报成 49.7 nm，
而实际暴露面只下降约 12 nm）。

记录形式（快照文档、`worker-result.json` 的步骤元数据、`<step>.meta.json` 中一致）：

```json
"measuredRemoval": {
  "material": "Si3N4",
  "medianHeightBefore": 0.0494, "medianHeightAfter": 0.0370,
  "topHeightBefore": 0.1060, "topHeightAfter": 0.0563,
  "medianHeightDelta": -0.0124, "topHeightDelta": -0.0497,
  "removedThickness": 0.0124, "thicknessBasis": "area-weighted-median",
  "removedCompletely": false, "remainingTopHeight": 0.0944,
  "method": "surface-mesh up-facing cells; no volume mesh",
  "materials": [ { "material": "Si3N4", "medianHeightBefore": 0.0494, "medianHeightAfter": 0.0370,
                   "topHeightBefore": 0.1060, "topHeightAfter": 0.0563,
                   "medianHeightDelta": -0.0124, "topHeightDelta": -0.0497,
                   "thicknessBasis": "area-weighted-median",
                   "removedThickness": 0.0124, "addedThickness": null,
                   "removedCompletely": false, "upFacingAreaBefore": 0.2566, "upFacingAreaAfter": 0.0397 } ]
}
```

厚度规则：

- 主厚度 = 主材料中位高度差；并且**不超过**该材料最高点的变化量（防止中位面整体跳到另一个
  平台：抛光掉顶层后残留在沟槽底部的同一材料会让中位面下移远超最高点）。
  被这一步限制时 `thicknessBasis` 写成 `highest-top (median surface changed)`。
- 最高点与中位高度都记录在记录和逐材料表里，UI 同时显示两者。
- 本步首次出现的沉积材料：`medianHeightBefore` 为 `null`，`thicknessBasis` 为
  `highest-top (new material)`。原因是 `Domain.duplicateTopLevelSet` 从当前最高 level set 开始生长，
  最高点恰好上升一个膜厚，而对“之前不存在”的材料没有可比较的中位面。

- 沉积步骤写 `measuredAdded.addedThickness`；刻蚀/平坦化/移除材料写 `measuredRemoval.removedThickness`。
- 主材料：沉积用 `depositMaterial`，`removeMaterial` 用 `material`，其余取本步实测去除厚度最大的材料
  （厚度即上文“中位高度差，且不超过最高点变化”的值）。
- 该步首次出现的沉积材料，其“变化前顶面”取上一步整个表面的最高点（对应
  `Domain.duplicateTopLevelSet` 从当前顶面开始生长），并标记 `materialAppeared: true`。
- 材料被完全移除时 `topHeightAfter`/`removedThickness` 为 `null`，`removedCompletely` 为 `true`，
  并记录该步之后整个表面的最高点 `remainingTopHeight`（不虚构一个厚度值）。
- 变化量小于 `0.25 × gridDelta` 的材料不进入列表，避免栅格噪声。

因此 ionBeam 步骤不再只能看 `planeWaferRate × duration`：那只是标定通量，不是线速度。
实测例子里 0.2 s / 0.05 µm·s⁻¹ 只去掉约 2 nm SiN，而 1.0 s 才清干净；现在这个数字会直接出现在
结果的“实测材料变化”里（`removedThickness` 与 `upFacingAreaAfter`）。

## 4. UI

- 步骤编辑器（平坦化）：`截断高度来源` 选择“固定高度”或“按材料停止 · <材料> 顶面”，
  后者显示 `过抛光量` 与限制说明。
- 结果：查看器信息条显示“实测去除/增加 <材料> <厚度>”，厚度按**面积加权中位暴露面**计算；
  原生结果页签给出中位与最高顶面的前后高度、`thicknessBasis`、有效截断高度（含 p90 方法与
  百分位）以及逐材料变化表。

## 5. 回归

`tests/test_cmp_measured_thickness.py`：

- `MaterialAwareCmpWorkflow`：真实原生 2D 流程（SiO2 场区 + 沟槽，TiN/W 覆盖，再按 SiO2 停止）。
  断言 CMP 后上表面 W 只留在沟槽开口内（长度 ≤ 1.2 µm、|x| ≤ 0.54）、场区没有 W/TiN 暴露，
  且 `measuredRemoval`（W 0.55 µm、TiN 0.05 µm）与 `measuredAdded`（TiN 0.05 µm、W 0.50 µm）成立。
- `PlanarizeDerivationUnit`：p90 停高推导公式、双峰选择（下方平台更大时仍选上方平台）、
  窄毛刺不影响结果、缺少停高表面时的明确错误、中位厚度与最高点差值的区别。
- `MaterialAwareCmpBimodalWorkflow`：真实原生 2D 双峰 SiO2（下方平台更大）+ TiN/W 覆盖，
  停止在 SiO2 时按上方平台切割。
- `PlanarizeStopMaterialValidation`：材料目录校验、运行时可见性校验、限制 warning，以及把
  `tests/fixtures/finfet22_project.json` 的第 11 步（W CMP）改写成 `stopMaterial: SiO2` 后仍可校验。
- `PlanarizeStopMaterialModel`：二选一约束与“未使用新字段时请求哈希输入不变”。
