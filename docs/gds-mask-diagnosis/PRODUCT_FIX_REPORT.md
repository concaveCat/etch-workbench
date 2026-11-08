# GDS 三维 stack 掩膜修复交付记录

日期：2026-10-29
工作区：`/tmp/viennaps-workbench-cpu-gpu-gds-interactive`（保留，未合并）
原工作区 `/root/viennaps-workbench-cpu-gpu` 未修改。

## 1. 根因与修复

根因见 `README.md` / `matrix.json`：`_apply_gds_stack_mask()` 曾用
`insertNextLevelSetAsMaterial(extruded, material, False)` 把只在 GDS 库网格上的
独立拉伸体追加为顶层，顶层因此只有 Mask；原生 Advect 的 `adjustLowerLayers`
会把 Si/SiO₂ 与顶层取交集，导致下层全部消失。直接把 `False` 改成 `True` 会在
不同网格的 UNION 中 SIGSEGV。

修复按 `EXECUTOR_TASK.md` 的指定算法实现：

1. 原生 `GDSGeometry.layerToLevelSet(layer, True)` 只读多边形内部，中间几何使用
   两个 `REFLECTIVE_BOUNDARY`，与最终计算域周期边界解耦；不再做二维 `INVERT`。
2. `worker/gds_mask.py::build_aligned_gds_mask()` 在 `[base, base+height]` 拉伸，
   用两个 `Plane` 重建上下封口并 `RELATIVE_COMPLEMENT` 成有限 slab，再与拉伸体
   `INTERSECT`。
3. 在深拷贝上 `ToSurfaceMesh(..., 0.01)` 取面，再用真实计算域网格
   `Domain(domain.getGrid())` + `FromSurfaceMesh`（`setRemoveBoundaryTriangles(False)`）
   重网格。周期方向由原生周期性保留，非周期方向丢弃域外三角面。
4. `outside` 用真实网格上 `GeometryFactory(...).makeSubstrate()` 建有限 target slab，
   再做 `target_slab RELATIVE_COMPLEMENT aligned`；反选在三维有限薄层上完成。
5. 最终为空时抛出 `GDS 掩膜在当前计算域内为空，请调整版图位置、保留区域或计算域。`。
6. 主入口改为
   `insertNextLevelSetAsMaterial(aligned_mask, material_handle(...), True)`，
   恢复 `LS0=Si`、`LS1=Si∪SiO₂`、`LS2=Si∪SiO₂∪Mask` 的嵌套不变量。

计算域、边界、`gridDelta`、`baseHeight`、掩膜厚度、材料、`protectsEtch` 与前端均未改动。

## 2. 文件清单

| 文件 | 变更 |
|---|---|
| `worker/gds_mask.py` | 新增：封口/重网格/反选 helper；本轮追加 `build_aligned_gds_layer()` |
| `worker/simulate.py` | 修改 `_apply_gds_stack_mask()`；本轮修改普通 `kind=gds` 2D/3D 图层插入 |
| `tests/fixtures/gds_stack_mask_probe.py` | 新增：调用产品 helper 的独立进程探针 |
| `tests/test_gds_stack_mask.py` | 新增：16 个物理/边界/极性回归 |
| `tests/test_gds_multi_material.py` | 本轮新增：2D/3D 普通 GDS 多材料操作流程回归 |
| `tests/fixtures/stage5_geometry_reference.py` | 本轮更新：直接参考采用同一嵌套/封口语义 |
| `tests/test_stage5.py` | 本轮修正过期的 GDS 资产元数据期望 |
| `docs/gds-mask-diagnosis/generic_gds_probe.py` | 本轮新增：不导入 Workbench 的原生嵌套证明 |
| `docs/gds-mask-diagnosis/run_generic_gds_checks.py` | 本轮新增：2D/3D × wrap 开关 4 案例 runner |
| `docs/gds-mask-diagnosis/product-acceptance/` | 新增：回归 JSON / guard / log 证据 |
| `backups/20261029-134055/simulate.py` | 第一次修改前备份 |
| `backups/20261029-141818/` | 本轮修改前备份（simulate.py、gds_mask.py、参考实现、test_stage5、旧 GPU summary） |

`worker/gds_mask.py` 不导入 `viennaps` 顶层包、不做文件 I/O 或材料注册、不调用
`Domain.getBoundingBox`。`extrudedPointCount` 仍为原始 Extrude 体点数。

新增诊断：`gridSource=simulation-domain`、`wrapLowerLevelSet=true`、
`alignedMaskPointCount`、`sourceSurfacePointCount`、`sourceSurfaceTriangleCount`、
`retainedRegion`、`construction=native-extrude-capped-surface-regrid-union-v1`。
`nativeGeometry` 如实包含 `ToSurfaceMesh`、`FromSurfaceMesh` 与 `UNION`。

## 3. 环境与基线

- Python 3.12.14；ViennaPS 4.6.2；ViennaLS 5.8.5（CPU，单线程）
- `/opt/eda-patches/verify.sh` 退出码 0（全部正常；未处于沙箱跳过状态）
- 基线：`./.venv/bin/python docs/gds-mask-diagnosis/run_canonical_checks.py`
  → 17/17 PASS，`canonical-acceptance/summary.json` passed=cases=17
- 未升级原生库，未修改网络 / license / EDA 安装

## 4. 测试结果

```bash
./.venv/bin/python -m unittest tests.test_gds_stack_mask -v
# Ran 1 test（16 个 subTest）... OK

./.venv/bin/python -m unittest tests.test_vtk_legacy tests.test_selectivity tests.test_geometry_operations -v
# Ran 6 tests ... OK

./.venv/bin/python -m unittest tests.test_3d -v
# Ran 6 tests ... OK
```

新测试覆盖：三种材料刻蚀后仍有表面且下层 level set 非零；计算域/边界/gridDelta
前后一致；方块内外取样 5.0 / 4.96（容差 1e-5）；outside 极性交换；环形孔洞内外；
全域覆盖 inside 有效；完全域外 inside 与全域覆盖 outside 均返回指定空掩膜错误；
反射跨边界只留域内；周期跨边界两侧保护且域外图形映回；非整除网格
`delta=.07, extent=5.07`；真实 GDS `delta=.1/.03`、`duration=.01/.2` 均不丢下层材料。

## 5. WebUI（8792）验收

请求文件：`var-ui-verify/gds-mask-fix/*.request.json`，响应：`*.response.json`。
final stepId 均为 `step_30aeb79d7ef049758fda719c`。

| 用途 | runId | 结果 |
|---|---|---|
| 修复后 GDS，delta=0.1 | `dcf47bcc8839a926a4229249` | succeeded，rc=0，4.8 s |
| 修复后 GDS，delta=0.03 | `0af8fbaf0ea442dd4ed38f19` | succeeded，rc=0，129 s |
| 无掩膜平面对照，delta=0.1 | `9c1207649daa4c106aa5b23d` | succeeded，rc=0，4.5 s |
| 修复前旧错误运行（参考） | `22b5ffe1169ae6d2799cb31a` | 最终 68 点 / 128 个 Mask 三角形，Si、SiO₂ 消失 |
| 重构后栈掩膜复验 | `a73391a19df1a31e29cd8490` | succeeded，5222 点，三材料齐全，裸露面 4.96 |

修复后检查（全部满足）：

- `returnCode=0`、`status=succeeded`，run.log 无 traceback/信号错误。
- Si(10)/SiO₂(30)/Mask(0) 在初始与最终快照中均存在；level set 索引映射仍为
  `[10, 30, 0]`。
- `delta=0.1`：最终 5222 点，裸露 SiO₂ 平面 623 个取样点为 4.960000 µm；
  Mask 体 z 范围为 4.96–5.27 µm（顶面重网格误差 ~0.03，小于任务书允许的
  2*gridDelta = 0.2）。
- `delta=0.03`：最终 57384 点，裸露平面 4.960000 µm，另有掩膜下保护点 5.000000 µm。
- 无掩膜对照：最终 5202 点，全部 625 个取样点为 4.960000 µm，顶面
  `surfaceBounds` 4.96。
- 三个运行的 `domainReadback` 初始/最终完全一致，网格未扩大或改粗；旧错误运行
  因下层消失导致域读数不一致。

说明：`delta=0.03` 在修复后需要输出约 233 MB 初始体网格与 212 MB 最终体网格，
本机用时 129 s，超过请求里原有的 120 s `solver.timeoutSeconds` 看门狗（第一次提交
`e248d952e6558451e8cbe41b` 因此 `timed_out`）。该字段只控制运行墙钟，不属于
requestedInputHash（两次提交 hash 均为 `a15dd0fd...`），也不改变工艺/几何参数；
用于验收的 `mask-003-longtimeout.request.json` 仅把它放宽到 600 s，算法与网格未变。
`mask-003.request.json` 保持任务书原样留档。

## 6. 其余 `insertNextLevelSetAsMaterial` 审查

| 位置 | 结论 |
|---|---|
| `worker/simulate.py:1214`（普通 2D `geometry.kind=gds`） | 每个映射层独立 `wrap=False` 追加；二维语义不同，本轮不改，需独立原生证明 |
| `worker/simulate.py:1235`（普通 3D `geometry.kind=gds`） | 各层在 GDS 库网格上独立追加，`wrap=False` 使顶层只含最后插入层，存在同类下层丢失风险；属另一路径，本轮不改 |
| `worker/simulate.py:1010/1029`（boolean fin 掩膜） | 默认 `wrap=True`，几何来自同一 `setup` 网格，兼容 |
| `worker/simulate.py:1299`（渲染域） | 仅渲染用，按设计手动合并同材料内部界面 |
| `worker/probe_*.py`、`tests/fixtures/*reference*.py` | 开发/对照探针，非产品运行路径 |

## 7. 剩余风险 / 未覆盖

- 本轮只实际回归 CPU isotropic 路径；GPU 与粒子（singleParticle/多粒子）模型
  未运行，不得视为已通过。修复不依赖 CUDA。
- 普通 `geometry.kind=gds` 多材料路径（2D/3D）已在本轮修复，见第 8 节；剩余
  风险只剩原生 3D `layerToLevelSet` 的空体缺陷，产品已完全绕开该接口构造 3D 图层。
- 原生 `d3.GDSGeometry.layerToLevelSet` 对部分合法 `baseHeight`/`height` 返回空
  level set，属安装版本缺陷；产品 3D GDS 路径不再调用它，但其他直接使用该接口
  的脚本/参考实现仍需注意。
- `delta=0.03` 的体网格导出成本较高；若前端默认体网格保持开启，120 s 看门狗在
  该分辨率下偏紧，属性能/资源配置问题，与本修复的正确性无关。
- `buf_4` 在 0.1 网格下的小多边形不足两个网格间隔，因此未用它断言精细轮廓精度；
  轮廓尺寸容差按任务书为 ≤ 2*gridDelta，且取样点离轮廓至少两个网格。

## 8. 普通 GDS 多材料路径修复（本轮追加）

### 8.1 根因

`geometry.kind=gds` 的 2D/3D 分支逐层调用
`insertNextLevelSetAsMaterial(level_set, material, False)`，把每个 GDS 图层作为
独立体追加。顶层因此只包含最后一个图层；Advect 的 `adjustLowerLayers` 会把
之前图层裁到最后一层的轮廓内（2D）或直接清空（3D，level set 点数变为 0）。

3D 还有两个原生 `d3.GDSGeometry.layerToLevelSet` 问题：

1. 对若干合法 `baseHeight`/`height`（例如 `base=0.2,height=0.4`、`base=0.6,*`）
   确定性返回空 level set，产品会静默丢层；
2. 它跟随 GDS 库边界和网格，而不是调用方计算域。

`docs/gds-mask-diagnosis/generic_gds_probe.py` 用不导入 Workbench 的原生 API
证明：同一双层 GDS，`wrap=False` 时 2D Si 被裁到 ±0.5、3D Si level set 直接变 0；
`wrap=True` 时两者都保留完整轮廓。`run_generic_gds_checks.py` 4/4 PASS。

### 8.2 修复

- 2D：保留原生 `d2.GDSGeometry.layerToLevelSet`，改为 `wrap=True`。同一 GDS
  几何的所有图层共享原生网格，累计 UNION 网格兼容。
- 3D：不再调用有缺陷的 `d3.layerToLevelSet`。用一次
  `d2.GDSGeometry.layerToLevelSet(layer, True)` 读取每个图层的多边形内部，
  再由 `build_aligned_gds_layer()` 执行
  `Extrude → 上下封口 → INTERSECT → 原生 UNION`；`mask=True` 用同一网格上的
  有限 slab 减去多边形体，保持原生负片语义并保证表面闭合。
  `base + height` 统一 `round(..., 12)`，规避 `0.2+0.4 = 0.6000000000000001`
  触发的原生平面退化为空。
- 所有 3D 图层保持原生 GDS 网格（与改动前一致），不裁切用户计算域；只有 stack
  GDS 掩膜继续使用 `FromSurfaceMesh` 对齐到用户网格。

### 8.3 覆盖的操作流程

`tests/test_gds_multi_material.py` 通过真实 API → supervisor → worker 运行：

- 2D 双层：等向刻蚀 → 定向刻蚀 → 沉积 → 平坦化 → 布尔切槽；每一步 Si/SiO₂
  都在，Si 的 x 范围保持 -1.5…1.5（未被顶层裁切）。
- 3D 双层：同样五步，并特意使用会让原生 3D 接口返回空体的
  `baseHeight=0.6/height=0.4` 与 `baseHeight=0.2/height=0.4`。
- 3D `mask=True` 负片图层：等向刻蚀后 Mask 与下层材料均存在且未被清除。

结果：**3/3 OK**。`tests.test_stage5`（单层 GDS 原生/直接参考逐点一致）、
`tests.test_gds_stack_mask`（17+16 场景）、`test_3d`、`test_vtk_legacy`、
`test_selectivity`、`test_geometry_operations` 回归均通过；本轮合计
`19 tests OK`。

`tests/test_stage5.py` 的 GDS 资产元数据断言原先缺少后端已返回的
`layerDatatypes` / `maskLayerDatatypes` 字段；按当前 API 契约补齐期望，
不改几何或运行逻辑。

### 8.4 其余 `insertNextLevelSetAsMaterial`

| 位置 | 结论 |
|---|---|
| `worker/simulate.py` 2D/3D `kind=gds` | 已改 `wrap=True`（3D 走 caps helper） |
| `worker/simulate.py` stack GDS 掩膜 | 已改 `wrap=True` + 用户网格重网格 |
| `worker/simulate.py` boolean fin | 同 `setup` 网格，默认 `wrap=True`，安全 |
| `worker/simulate.py` 渲染域 | 仅渲染，按设计合并同材料内部界面 |
| `probe_*` / `*reference*` / 测试 fixture | 开发或直接参考路径，非产品运行 |

## 9. GPU / 粒子模型回归（本轮追加）

### 9.1 GDS 多材料 GPU 矩阵

运行：

```bash
VIENNAPS_PATCHED_RUNTIME_DIR=$PWD/runtime-patched/current \
  ./.venv/bin/python tests/fixtures/run_gpu_gds_multilayer_probe.py
```

结果：**80/80 verified**，`docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/summary.json`
`passed=true`。覆盖：

- 维度：2D、3D
- 模型：`singleParticle`、`multiParticle`（含原生多离子与 rate callback）
- 工艺：`etch`、`deposit`
- 边界：reflective、periodic
- 图层组合：film/film、mask/mask、film/mask（含同材料/不同材料/自定义材料
  `GdsGpuCustom*`）
- 每个案例同时运行产品 worker 与独立直接参考，初始/最终点、单元、材料 ID、
  level-set 映射、GPU 引擎、所选图层/结构、过滤 GDS 哈希全部逐项一致。

### 9.2 本轮为 GPU 周期案例做的两处修正

1. 2D GDS 的 `unitScale=1.5` 使结构 x 跨度为 0…6.75 µm；周期域 xExtent=8
   时原始 position 会让结构超出周期。探针改为把该结构平移到
   `position=[-3.35, 0.1, 0]`，使其完整落在周期内；这是修正无效的测试布置，
   不改产品周期检查。
2. 3D 普通 GDS 图层现在按**仿真域的实际侧向边界**构造（周期项目用周期边界）。
   此前中间 2D 几何固定用 reflective，插入后会把周期域的边界读回改成
   reflective，触发 `Domain boundary readback ... differs`。stack GDS 掩膜仍
   按任务书使用中间 reflective + 最终重网格，不受此修改影响。

### 9.3 其他 GPU/粒子测试

- `tests.test_gpu_device_selection`、`tests.test_stage7`、`tests.test_stage8`：
  **OK**
- `tests.test_stage9`：**27/28 OK**。唯一失败
  `test_patched_gpu_multiparticle_multi_ion_support_is_evidence_backed` 缺少
  `docs/.../profile-recheck/cpu-nan-debug/worker.guard.json`；在原工作区
  `/root/viennaps-workbench-cpu-gpu` 运行同一测试也失败，属工作区原有证据缺口，
  与本轮代码无关。
- `tests.test_api_integration`：14/15 OK（4 skipped）；唯一失败是 1 MiB 请求体
  期望 413 的中间件断言，属既有 API 契约漂移，与本轮 GDS/GPU 改动无关。
