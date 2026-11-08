# ViennaPS 示例预设（对接用）

数据文件：[`viennaps-example-presets.json`](./viennaps-example-presets.json)

来源：`/root/ViennaPS/examples`，commit `7a8b17f`（2026-09-18）。本文件只记录参数与映射，不运行、不复制 GDS 二进制；另一个 session 负责接入 UI/引擎。

## 1. 预设一览

| id | 示例 | 维度 | 关键模型 | 主要缺口 |
|---|---|---|---|---|
| `trench-deposition` | Trench Deposition | 2D/3D | `singleParticle` deposit | 3D 需要 yExtent |
| `sf6o2-hole-etching` | SF6/O2 Hole Etching | 2D/3D | `specialized: sf6o2` | A_Si=7、quarter hole 暂不可表达 |
| `bosch-process` | Bosch Process | 3D | 循环 `singleParticle` + `multiParticle` | StepV7 无循环节点；自定义 rate function |
| `wet-etching-cantilever` | Wet Etching | 3D only | `specialized: wetEtch` + GDS mask | 需要 `cantilever_mask.gds`；7200 s > 600 s 上限 |
| `selective-epitaxy` | Selective Epitaxy | 2D/3D | `specialized: selectiveEpitaxy` | Fin + oxide 平面不能叠加 |
| `oxide-regrowth` | Redeposition During Selective Etching | 3D | `specialized: oxideRegrowth` | 需要 DenseCellSet；923 s > 600 s |
| `fin-oxidation` | Fin Oxidation | 2D/3D | `specialized: oxidation` | timeStep 源码未显式给 |
| `locos-oxidation` | LOCOS Oxidation | 2D | `specialized: oxidation` | 半掩膜 + mask mechanics 未建模 |
| `gds-mask-import` | GDS Mask Import | 2D/3D | GDS geometry | 需要 `myTest.gds` 资产；3D GDS API 限制 |

## 2. 对接顺序建议

1. **先把预设作为只读数据接入 01 示例库**：卡片显示名称、维度、模型、缺口标签；不要直接生成可运行工程，避免“看起来能跑但参数不完整”。
2. **补齐 ProjectV7 映射**：
   - `specialized` 步骤必须写 `model:"specialized"`、`nativeModel`、`nativeParameters`，字段集合必须与 `backend/specialized_models.py` 完全一致。
   - `duration` 单位秒；`oxidation` 不写 `duration`，时间来自 `nativeParameters.timeHours`。
   - `depositMaterial` 只用于 deposit/native deposition；etch 步骤不要带。
3. **3D GDS**：
   - 后端 `GDSGeometryV7` 已支持 3D，每层需要 `baseHeight`/`height`。
   - `POST /clips/{id}/compile` 目前显式拒绝非 2D；需要放开，或在 01 用 `/flows/from-project` 直接提交 3D GDS geometry。
   - 3D GDS 图层映射参考 `gds-mask-import` 的 `layers3D`。
4. **时长上限**：`StepV7.duration` 最大 600 s。wet etching 7200 s、oxide regrowth 923 s 需要：
   - 拆成多个步骤/循环，或
   - 扩展 schema 上限（需引擎/验证同步）。
5. **循环**：Bosch 10 个循环需要 flow definition/模块循环；当前 02 的平铺步骤只能展开成 20 步。
6. **GDS 资产**：`cantilever_mask.gds`、`myTest.gds` 在 `/root/ViennaPS/examples/...`；不要直接提交 GPL 示例二进制到公开仓库，接入时由用户上传或放到 `var/` 运行时资产。
7. **缺失物理字段**：
   - `sf6o2` 的 `A_Si`（Substrate.A_ie）当前 catalog 无字段。
   - Bosch 自定义 rate function 当前 multiParticle schema 无表达。
   - LOCOS 的 mask mechanics、半掩膜几何当前 ProjectV7 无表达。
   - Selective Epitaxy 的 Fin + oxide 平面需要几何扩展。

## 3. 每个预设的源码依据

- Trench Deposition：`examples/trenchDeposition/config.txt`、`trenchDeposition.py/.cpp`
- SF6/O2：`examples/holeEtching/config.txt`、`holeEtching.py`
- Bosch：`examples/boschProcess/config.txt`、`configBoschRayTracing.txt`、`boschProcessEmulate.py`、`boschProcessSimulate.py`
- Wet Etching：`examples/cantileverWetEtching/cantileverWetEtching.py`、`cantilever_mask.gds`
- Selective Epitaxy：`examples/selectiveEpitaxy/config.txt`、`selectiveEpitaxy.py`
- Oxide Regrowth：`examples/oxideRegrowth/config.txt`、`oxideRegrowth.py`
- Fin Oxidation：`examples/finOxidation/config.txt`、`finOxidation.py`
- LOCOS：`examples/locosOxidation/config.txt`、`locosOxidation.py`
- GDS Mask Import：`examples/GDSReader/GDSReader2D.py`、`GDSReader.py`、`myTest.gds`、`mask.gds`

## 4. 单位换算

源码混用 `um`、`nm`、`min`、`hour`；JSON 保留源码单位并在 `sourceUnits`/`sourceParameters` 标注。ProjectV7 映射统一：

- 长度：µm
- 时间：s
- 速率：µm/s
- 角度：deg

换算示例：

- holeEtching `processTime=1 min` → `60 s`
- wet etching `r100=0.797/60` → `0.0132833 µm/s`
- oxide regrowth `gridDelta=2 nm` → `0.002 µm`，`layerHeight=30 nm` → `0.03 µm`
- oxide regrowth `nitrideEtchRate=6.5 nm/min` → `0.00010833 µm/s`

## 5. 不在本文件里的内容

- 不包含 GDS 二进制、图片、源码副本。
- 不包含“可直接运行”的完整 ProjectV7 文件；因为部分示例依赖当前 schema 尚未表达的几何/循环/力学字段。
- 不包含 UI 卡片文案；另一个 session 接入时再本地化。
