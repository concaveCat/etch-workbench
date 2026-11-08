# 各向同性步骤分流合同（`isotropicMethod` / `isotropicTimeStepRatio`）

本合同适用于 schema 4–7 的 `model: "isotropic"` 步骤。所有结论来自安装版
ViennaPS 4.6.2 / ViennaLS 5.8.5 的独立子进程测量与本仓库的 worker 实现；
没有改动 ViennaPS 引擎，也没有启动正式服务。

| 项目 | 值 |
| --- | --- |
| 引擎 | viennaps 4.6.2 / viennals 5.8.5 |
| 执行后端 | `CPU_DISK`（几何分布同样显式固定，避免默认 `AUTO` 初始化 CUDA） |
| 测量线程 | 2D/3D 平面基准为 `setNumThreads(2)`；复杂表面为同批 CPU 单次测量 |
| 测量方式 | 同一进程内分别构造新 Domain，单次 `Process.apply()` 墙钟；同批对比，跨批次绝对值会漂移 |
| 日期 | 2026-11-01 |

## 1. 两条原生路径不是同一件事

`isotropic` 步骤现在可以在两条完全不同的原生实现之间分流。

### 1.1 `ps.IsotropicProcess`：PDE / 速度场推进

- 输入是**带符号速率**（µm/s）和**持续时间**（s），由 level-set 方程
  `∂φ/∂t + v|∇φ| = 0` 推进表面；位移约等于 `rate × duration`。
- 支持逐材料速率表（`rateMode: "material"`）、几何掩膜材料、`timeStepRatio`
  控制的 CFL 子步。
- 表面随时间连续演化，能表达“速率 × 时间”的物理量；不是一次形态学偏移。
- worker 显式固定 `CPU_DISK`；该模型不使用射线通量引擎。

### 1.2 `ps.SphereDistribution`：几何偏移

- 它是 `ProcessModelCPU` 对 `viennals::GeometricAdvect` 的封装，输入只有一个
  **带符号半径**：正数沉积、负数刻蚀；欧氏距离推进。
- `ps.Process(domain, model, duration)` 的 `duration` **完全不参与计算**；几何推进
  一次完成，结果只由半径（以及掩膜）决定。
- 沉积只推进顶层/暴露 level set；刻蚀后会把下层 level set 与新表面对齐。
- 支持带掩膜刻蚀（`addMaskMaterial`，官方 FinFET 示例同一条路径）；
  **带掩膜沉积在 2D/3D 都会 SIGSEGV**，因此工作台只在刻蚀时传掩膜。
- 不能表达逐材料速率，也不能表达时间演化；`rateMode: "material"` 与几何路径互斥。

因此“各向同性”在本工作台有两种落地：

| 请求 | 原生类 | 位移来源 | `duration` | 材料速率表 |
| --- | --- | --- | --- | --- |
| `isotropicMethod: "levelSet"`（或缺省） | `IsotropicProcess` | `rate × duration`，PDE 演化 | 参与 | 支持 |
| `isotropicMethod: "geometric"` | `SphereDistribution` | 一次几何半径 | 忽略 | 不允许 |

## 2. 步骤字段

字段定义在 `backend/models.py:StepV4`，由 schema 5/7 的 `StepV5` / `StepV7`
继承；只对 `model: "isotropic"` 有效。

```json
{
  "id": "step_iso_0001",
  "name": "ILD 沉积",
  "enabled": true,
  "operation": "deposit",
  "model": "isotropic",
  "rateMagnitude": 0.06,
  "duration": 2.0,
  "depositMaterial": "SiO2",
  "rateMode": "uniform",
  "isotropicMethod": "auto"
}
```

| 字段 | 必选 | 约束 | 语义 |
| --- | --- | --- | --- |
| `isotropicMethod` | 否 | `auto` / `levelSet` / `geometric`；只允许 `model: "isotropic"` | 缺省等价于旧行为 `levelSet` |
| `isotropicTimeStepRatio` | 否 | `0.05`–`0.5`；只允许 `model: "isotropic"` | 只作用于 level-set PDE；缺省使用 `solver.timeStepRatio` |

两个字段缺省时不会出现在 `model_dump(exclude_none=True)`、标准化项目或请求哈希中，
因此旧项目/旧 replay 的请求哈希逐字节不变。显式写入 `isotropicMethod: "auto"`
则会参与请求哈希，因为它是真实的请求差异。

字段级校验：

- 出现在非 `isotropic` 步骤上 → 422；
- `isotropicTimeStepRatio` 超出 `0.05..0.5` → 422；
- `isotropicMethod: "geometric"` + `rateMode: "material"` → 422（几何路径只有一个
  均匀半径，无法表达材料表）。

## 3. 自动分流规则（`auto`）

`auto` 只在**既安全、又在本仓库的测量中不处于明显劣势**时选择几何路径；其余情况
一律 level-set。

| 条件 | `auto` 的结果 |
| --- | --- |
| 项目维度 = 2 | 满足下面所有安全条件时选 `geometric` |
| `rateMode = "uniform"` | 必要条件；材料速率表只能走 level-set |
| `thickness = rateMagnitude × duration ≥ gridDelta` | 必要条件；低于一个网格间距的几何偏移无法被稀疏 level set 解析 |
| `operation = "deposit"` 且几何没有掩膜材料 | 必要条件；正位移 + 非空掩膜会崩溃 |
| `operation = "etch"` | 允许几何掩膜；掩膜由 `addMaskMaterial` 保护 |
| 项目维度 = 3 | **始终 `levelSet`**，即使几何路径在安全条件内 |

3D 的 `auto` 不选择几何不是保守，而是实测结论：在 22 nm 单元 / 5 nm 网格上，
`SphereDistribution` 比 `IsotropicProcess` 慢 3–30 倍，平面几何也一样；更细的
2.5 nm 网格上落差更大（见 §5）。让 `auto` 静默选择更慢的路径没有意义。

`auto` 的分流原因会写入 `actualParameters.reason`，实际选择写入
`actualIsotropicMethod`；请求值写入 `requestedIsotropicMethod`。因此即使同一工程
在不同维度下 `auto` 走不同路径，运行记录也能解释原因。

## 4. 显式覆盖与安全校验

### 4.1 显式 `levelSet`

任何 `isotropic` 步骤都可以显式请求 `levelSet`。它保持旧行为：

```python
ps.IsotropicProcess(rate=signed_rate, maskMaterials=mask_handles)
ps.Process(domain, model, duration)
```

- 允许 `rateMode: "material"`（沉积和刻蚀都允许；见 §7）；
- 允许几何掩膜；
- `isotropicTimeStepRatio` 生效并记录。

### 4.2 显式 `geometric`

显式几何路径是**受控覆盖**，不是默认优化。必须同时满足：

- `rateMode = "uniform"`，且没有 `materialRates`；
- `thickness = rateMagnitude × duration ≥ gridDelta`；
- 沉积：几何声明的掩膜材料必须为空（正位移带掩膜不安全）；
- 刻蚀：允许掩膜，掩膜材料来自项目几何（trench/hole/fin 的 `maskMaterial`、
  stack 的 `role=mask` 图层、GDS mask 图层）；域里不存在的掩膜材料会记为
  `omittedMaskMaterials`，不会静默当成“已保护”；
- 3D 显式几何允许执行，但 `/api/projects/validate` 会返回警告：同一位移下通常比
  level-set 慢 3–30 倍，仅作为有意覆盖使用。

不满足以上条件的显式 `geometric` 在验证阶段直接 422，而不是让 worker 走到原生
崩溃或静默 no-op。

### 4.3 几何路径的 no-op 检查

几何偏移是形态学操作，可能因为“掩膜覆盖整个暴露表面”而合法地什么都不动。worker
在调用前后各取一次表面签名（坐标、拓扑、`MaterialIds`、材料映射），只有签名相同才
算 no-op；此时步骤失败并给出明确错误，而不是产出一份“没有变化”的快照。成功步骤的
快照/预览与 level-set 路径完全一致，逐步骤保留，不会因为分流而省略。

## 5. 实测基准

### 5.1 2D：几何路径在厚膜上明显更快

2D 平面，`gridDelta = 0.005 µm`，`xExtent = 0.5 µm`，`setNumThreads(2)`，
`CPU_DISK`；几何为 `SphereDistribution(+thickness)`，level-set 为
`IsotropicProcess(rate=thickness / duration)`、`timeStepRatio = 0.25`。
`ratio = sphere / isotropic`。

| 膜厚 | 几何 `SphereDistribution` | level-set `IsotropicProcess` | ratio |
| ---: | ---: | ---: | ---: |
| 5 nm | 0.001 s | 0.001 s | 1.39 |
| 50 nm | 0.000 s | 0.003 s | 0.10 |
| 150 nm | 0.001 s | 0.011 s | 0.05 |

2D 的几何推进成本只与二维网格和表面点相关，绝对耗时在毫秒级；50/150 nm 时几何
快约 10–20 倍。5 nm 薄片两者都在 1 ms 量级，差异落在计时噪声内，所以 `auto` 在
2D 选几何不会带来可感知成本。表中的 `0.000 s` 表示低于 1 ms 的显示精度，ratio 仍按
原始浮点计算，不要把三位小数当成分辨率。

带掩膜 2D 刻蚀（几何 `SphereDistribution(-thickness)` + `addMaskMaterial(Mask)`，
level-set 同样保护 `Mask`）的测量更接近：

| 膜厚 | 几何 | level-set | ratio |
| ---: | ---: | ---: | ---: |
| 5 nm | 0.0011 s | 0.0007 s | 1.55 |
| 50 nm | 0.0017 s | 0.0016 s | 1.06 |
| 150 nm | 0.0015 s | 0.0006 s | 2.66 |

这些数都在 1–2 ms，掩膜和计时抖动占主导；它们说明 2D 的收益主要来自**无掩膜、
较厚**的均匀膜，而不是“2D 几何永远更快”。`auto` 的 2D 条件因此仍以安全规则
（uniform、厚度、沉积无掩膜）为准，不把薄片/掩膜当成性能承诺。

### 5.2 3D：几何路径在我们的工况下更慢

3D 平面，`gridDelta = 0.005 µm`（5 nm），`0.5 × 0.5 µm`，同批测量：

| 膜厚 | 几何 `SphereDistribution` | level-set `IsotropicProcess` | ratio |
| ---: | ---: | ---: | ---: |
| 5 nm | 0.414 s | 0.019 s | 22.29 |
| 50 nm | 0.798 s | 0.162 s | 4.92 |
| 150 nm | 2.031 s | 0.618 s | 3.29 |

3D 复杂表面（约 148k 表面点，同批测量）同样不占优：

| 膜厚 | 几何 | level-set | ratio |
| ---: | ---: | ---: | ---: |
| 5 nm | 9.35 s | 0.39 s | 24 |
| 50 nm | 10.4 s | 3.56 s | 2.93 |
| 150 nm | 18.7 s | 9.1 s | 2.06 |

3D “接近官方示例”的尺寸：`gridDelta = 0.00079 µm`（0.79 nm），
`0.09 × 0.1 µm`，4 nm / 50 nm 膜厚：

| 膜厚 | 几何 | level-set | ratio |
| ---: | ---: | ---: | ---: |
| 4 nm | 1.112 s | 0.156 s | 7.12 |
| 50 nm | 7.079 s | 1.482 s | 4.78 |

更细的 3D 网格（`gridDelta = 0.0025 µm`，`0.5 × 0.5 µm`）也没有反转：

| 膜厚 | 几何 | level-set | ratio |
| ---: | ---: | ---: | ---: |
| 4 nm | 5.656 s | 0.155 s | 36.44 |
| 50 nm | 17.703 s | 1.249 s | 14.18 |

结论：**在 3D 的 5 nm / 0.79 nm / 2.5 nm 网格上，几何路径在 3–30 倍区间内更慢，
个别更细网格达到约 36 倍；没有任何一行在 3D 中快于 level-set。** `auto` 因此在
3D 永远选 level-set；显式 `geometric` 只是留给“确实需要一次形态学偏移”的覆盖，
并且验证会给出慢路径警告。

> 测量口径：单次墙钟、同批对比，跨批次会漂移（同一配置在不同批次可能差 10–20%）。
> 这些表用于分流决策，不是性能 SLA；不要用单次绝对值给某个模型下“更快”的结论。

## 6. `isotropicTimeStepRatio`

`isotropicTimeStepRatio` 是**步骤级**覆盖，只对 level-set PDE 生效；缺省时使用
`solver.timeStepRatio`。它不会改变几何路径（几何路径没有 PDE 子步，显式写该字段
会记录为 ignored）。`auto` 解析为 geometric 时同样忽略该字段；解析为 levelSet 时
按下面规则生效。

3D ILD 状态上的同批测量（CPU，单步，其余输入相同）：

| `timeStepRatio` | 单步耗时 | 说明 |
| ---: | ---: | --- |
| 0.25（默认） | 17.5 s | 基准 |
| 0.5 | 8.7 s | 约 2× 加速 |

0.5 是允许的上限，不是“推荐值”。CFL 时间步变大意味着更少的子步；在深沟槽、
薄侧墙、尖角或强各向异性速度场里可能欠解析，导致轮廓偏差。工作台只负责把请求
值真正传给原生 `AdvectionParameters` 并记录：

- 生效时 `actualParameters.timeStepRatio` 是实际值；
- `actualParameters.timeStepRatioSource = "step.isotropicTimeStepRatio"`；
- 同时记录 `solverTimeStepRatio` 作为对照；
- 几何路径记录 `timeStepRatio: "ignored: geometric advection integrates no PDE"`。

建议先用默认 0.25 建立基线，再用 0.5 重跑同一工程并比较快照/实测厚度；不要只看
墙钟。`isotropicTimeStepRatio` 不是精度开关，调大不会自动提高精度。

## 7. 各向同性材料速率表（与分流的关系）

`rateMode: "material"` 现在对**沉积和刻蚀都开放**（旧版本只允许刻蚀）。语义：

- `rateMagnitude` 是**未列出材料的默认速率**；默认 0 会保护未列出的材料；
- `materialRates` 是非负速率大小，实际符号由 `operation` 决定（刻蚀为负、沉积为正）；
- 原生调用仍是
  `ps.IsotropicProcess(rate=default, maskMaterials=[])` +
  `setMaterialRate(handle, signed_rate)`；
- 只允许走 level-set；`isotropicMethod: "geometric"` 或 `auto` 解析到几何时都不允许
  材料表（`auto` 会自动回退到 level-set）。

因此材料速率表与分流的关系可以概括为：**有材料表 → 只能 level-set**。几何路径只
接受一个均匀半径。

## 8. 执行记录、产物与 replay

### 8.1 `actualParameters`

分流生效时（步骤显式写了 `isotropicMethod` 或 `isotropicTimeStepRatio`），
`manifest.actualExecution[].actualParameters` 记录下表字段。两个字段都缺省的旧步骤
不新增这些键，保持旧 manifest 的形状；`requestedIsotropicMethod` 在只写
`isotropicTimeStepRatio` 时按 `levelSet` 记录。

| 键 | level-set | geometric |
| --- | --- | --- |
| `requestedIsotropicMethod` | 请求值（缺省记录为 `levelSet`） | 请求值 |
| `actualIsotropicMethod` | `levelSet` | `geometric` |
| `nativeClass` | `IsotropicProcess` | `SphereDistribution` |
| `nativeConstructor` | — | `viennaps.d2/d3.SphereDistribution(radius)` |
| `nativeRadiusUm` | `null` | 带符号半径（沉积正、刻蚀负） |
| `thicknessUm` | `rateMagnitude × duration` | 同左 |
| `cells` | `thickness / gridDelta` | 同左 |
| `gridDeltaUm` | 网格间距 | 同左 |
| `maskMaterials` | 实际传入的原生掩膜材料 | 实际 `addMaskMaterial` 的材料 |
| `omittedMaskMaterials` | — | 几何声明但域里不存在、未传入的掩膜材料 |
| `reason` | 分流原因 | 分流原因 |
| `nativeDurationIgnored` | `false` | 被忽略的请求持续时间（秒） |
| `timeStepRatio` | 实际生效值 | `"ignored: ..."` |
| `timeStepRatioSource` | `step.isotropicTimeStepRatio` 或 `solver.timeStepRatio` | — |
| `nativeProcessBackend` | — | `CPU_DISK pinned; ...` |

几何路径另外记录调用前后的 `surfaceSignatureBefore` / `surfaceSignatureAfter` 与
`surfaceBoundsBeforeUm` / `surfaceBoundsAfterUm`；签名相同即判定 no-op 并失败。

材料速率表（level-set）还会记录：

- `rateMode`、`actualDefaultRate`、`actualMaterialRatesByName`、
  `actualMaterialRatesById`、`nativeRateTable`；
- `requestedDefaultRate`、`requestedMaterialRatesByName`、`operationSign`。
  这样“请求的非负表”和“实际传给原生的带符号值”都能审计。

`step-configured` 事件在分流生效时额外带顶层 `isotropicMethod` 与 `nativeClass`，
方便前端/历史列表直接显示实际路径。

### 8.2 快照、预览与 replay

- **逐步骤快照和预览不因分流而省略**：level-set 与几何路径在成功步骤后都按原流程
  生成 `snapshot`/VTK/历史记录；失败步骤仍然报错，不产出“零变化”结果。
- 新字段只是项目 JSON 的可选字段，继续通过既有的 `/api/projects`、`/api/runs`、
  `/api/runs/{runId}/replay-project`、`/api/runs/replay` 传递；没有新端点或新资产。
- 旧项目缺少两个字段时，标准化 JSON、请求哈希与 replay 行为保持不变；新项目写入
  `auto` 或显式方法时，请求哈希包含该选择，replay 会复用同一请求。
- `auto` 的实际路径取决于项目维度与 `rateMagnitude × duration`；同一项目在 replay
  中维度/步骤不变，因此实际路径可复现，且 `actualIsotropicMethod` 会写入运行记录。

## 9. 不声称的部分

- 几何路径不是物理沉积/刻蚀模型：没有通量、黏附概率、角度分布、材料选择比或
  时间演化；位移是参数值，不是 `rate × duration` 的连续解。
- 3D 几何路径虽然允许显式覆盖，但不推荐用于常规流程；实测在本仓库的 3D 网格上
  始终慢于 level-set。
- `isotropicTimeStepRatio = 0.5` 的 ~2× 加速来自特定 ILD 状态；不是所有几何/所有
  网格都成立，也不保证精度不变。
- 2D 几何路径的收益依赖膜厚和掩膜；薄片/带掩膜时两者可能同量级，`auto` 的规则
  仍以安全和均匀速率为准。
