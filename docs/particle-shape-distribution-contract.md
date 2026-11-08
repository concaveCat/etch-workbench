# 粒子形状分布合同（schemaVersion 7 / `particleShape`）

本合同描述安装版本 ViennaPS 4.6.2 + ViennaLS 5.8.5 中
`BoxDistribution`、`SphereDistribution`、`CustomSphereDistribution` 的实际行为，
以及工作台暴露了其中哪一部分。所有结论都来自**独立子进程**探测
（`scripts/bounded_child.py` + `worker/probe_particle_distributions.py` /
`worker/probe_particle_mask.py`），没有接触正式服务、正式数据目录或
`docs/*-evidence/`。

| 项目 | 值 |
| --- | --- |
| 引擎 | viennaps 4.6.2 / viennals 5.8.5（交叉验证：4.7.0 / 5.10.0） |
| Python | 3.12.14 |
| 工作台 | project schema 7，分支 `task/a4-mask`，基线 `1015266`（A4 首个提交基于 `afe9f46`） |
| 探测日期 | 2026-10-30 |
| 方法 | 每个用例一个 `bounded_child.py` 子进程，40–60 s 超时、2–4 GiB RSS 阈值、日志上限 1 MiB；平面/沟槽/掩膜盒/叠层小域，`gridDelta=0.05`、`setNumThreads(1)`；崩溃用例另用 `gdb -batch` 取回溯 |

## 1. 结论

| 原生类 / 组合 | 处理 | 原因 |
| --- | --- | --- |
| `BoxDistribution`（无掩膜） | **暴露**，schema 7 几何步骤 `particleShape` + `shape: box` | 2D/3D 都只改变几何，位移等于半轴的最大范数（L∞），可复现 |
| `SphereDistribution`（无掩膜） | **暴露**，`particleShape` + `shape: sphere` | 2D/3D 位移等于半径，可复现 |
| **带掩膜的 `Box`/`Sphere` + 负位移（刻蚀）** | **暴露**：`operation: etch` 时按项目几何的原生掩膜材料调用 `addMaskMaterial` | 与官方 `examples/emulation/FinFET.py` 完全同一条路径；2D/3D 都成功，掩膜材料不动、其余表面收缩 |
| 带掩膜的 `Box`/`Sphere` + 正位移（沉积） | **不暴露**（沉积步骤始终使用无掩膜重载，并在运行记录里列出被忽略的掩膜材料） | 任何**非空**掩膜的沉积在 2D/3D 均 `SIGSEGV`（returnCode `-11`），与掩膜构造方式、是否与表面重叠无关；ViennaLS 5.8.5 与 5.10.0 源码里都保留着 “There are still issues with positive box distributions if there is a mask used!” 的 TODO |
| `CustomSphereDistribution` | **不暴露** | 无掩膜即 `std::terminate`（`-6`）：它按表面点 id 索引半径，JSON 无法安全表达 |

因此 A4 的落地是：**无掩膜沉积 / 带掩膜刻蚀的几何偏移步骤**。掩膜不是新字段，
而是沿用工作台其它步骤已经使用的项目几何原生掩膜（`protected_material_names`：
trench/hole/fin 的 `maskMaterial`、stack 的 `role=mask` 图层、GDS 的 mask 图层）。

> 旧结论“所有掩膜路径都不可用”来自上一轮探测：那批用例把掩膜也用在**沉积**上，
> 又只测了负位移会静默不动的构型。本轮按官方 FinFET 的调用签名重测后，带掩膜刻蚀
> 在 2D/3D 都稳定、可定位，因此本轮把它接进工作台；沉积仍然被原生缺陷挡住。

## 2. 它们到底是什么

`viennaps.BoxDistribution` / `SphereDistribution` 不是通量或速度模型，而是
`ProcessModelCPU` 对 `viennals::GeometricAdvect` 的封装：

- `BoxDistribution(halfAxes)` 用 L∞（切比雪夫）距离推进表面；
- `SphereDistribution(radius)` 用欧氏距离推进表面；
- 正参数 = 沉积（沿外法线生长），负参数 = 刻蚀（沿内法线收缩）；
- `ps.Process(domain, model, duration)` 的 `duration` **完全不参与计算**：
  `GeometricProcessStrategy` 直接调用 `GeometricAdvect(surface, dist, mask)`；
- 沉积只推进**顶层/暴露** level set：叠层中只有最上层界面移动；
- 刻蚀后 `GeometricProcessStrategy` 会把所有下层 level set 与新的表面对齐
  （`INTERSECT`），因此刻蚀会真实消耗材料；
- 盒分布在 2D 只使用前两个半轴；原生绑定仍要求固定长度 3 的数组，
  工作台把第三个位置填 `0.0`（原生在 2D 不读取它）。

## 3. 掩膜路径：官方用法与实测矩阵

官方 4.6.2 例子（`examples/emulation/FinFET.py`）只对**刻蚀**加掩膜，签名如下：

```python
box = ps.ls.Domain(domain.getGrid())
ps.ls.MakeGeometry(box, ps.ls.Box(minPoints, maxPoints)).apply()
domain.insertNextLevelSetAsMaterial(box, ps.Material.Mask)

dist = ps.BoxDistribution(halfAxes=[-gridDelta, -gridDelta, -etchDepth])
dist.addMaskMaterial(ps.Material.Si)      # 保护 Si
ps.Process(domain, dist).apply()          # 两参数重载；三参数 duration 结果相同
```

`addMaskMaterial(material)` 的语义是**保护**该材料：`GeometricProcessStrategy`
用材料 level set 造一个 `viennals` 掩膜（对该材料减去更低的非掩膜 level set），
再交给 `GeometricAdvect`，落在掩膜内的表面点不移动。

材料句柄（4.6.2，3D 与 2D 相同）：

| 来源 | 结果 |
| --- | --- |
| `ps.Material.Mask` | `Material('Mask')`，`kind()=0`、`legacyId()=0`、`isBuiltIn()=True`、`isCustom()=False` |
| `ps.MaterialRegistry.instance().findMaterial("Mask")` | 与 `ps.Material.Mask` **相等**（`==` 为真） |
| `ps.MaterialRegistry.instance().getMaterial("Mask")`（工作台 `material_handle` 用的就是这个） | 同上，同一个原生句柄 |
| 域材料表（trench / 掩膜盒） | level set 顺序 `[Material('Si'), Material('Mask')]`，`legacyId` `[10, 0]` |

自注册材料（`registerMaterial`）作为 `maskMaterial` 也能正常进入
`addMaskMaterial`，`getMaterialLevelSet` 同样能取到它的 level set（见 §9 的自定义
材料用例）。

### 3.1 可用的组合（全部 returnCode `0`）

| 用例 | 维度 | 结果 |
| --- | --- | --- |
| 材料掩膜 `addMaskMaterial(Mask)`，负位移，平坦面上的掩膜盒 | 2D / 3D | 掩膜顶面 `z=0.1999` 不动，掩膜外的 Si 表面从 `0` 降到 `-0.15`（盒） |
| 材料掩膜 `addMaskMaterial(Mask)`，负位移，`MakeTrench` 沟槽 | 2D / 3D | 掩膜顶面不动（`0.1999`），沟底 `-0.425 → -0.55` |
| 材料掩膜，负位移，`ps.Process(domain, model, 1.0)` | 2D / 3D | 与 `0.0` / 两参数重载**逐位相同** |
| 原始 `viennals` 掩膜：`ps.BoxDistribution(halfAxes, ps.ls.Domain(domain.getMaterialLevelSet(Mask)))` | 2D / 3D | 与材料掩膜坐标逐位相同 |
| `model.applyToSingleMaterial(Si)`，负位移 | 2D / 3D | 与材料掩膜坐标逐位相同 |
| 多个掩膜材料（`Mask` + `Si3N4`） | 2D / 3D | 成功；两者都受保护时表面不动（工作台会因此报错，见 §8） |
| 掩膜远离表面（盒子在 `z=1.2…1.8`，表面 `z=0`），负位移 | 2D / 3D | 成功，整个表面按半径收缩 |

### 3.2 不可用的组合

| 用例 | 维度 | 结果 |
| --- | --- | --- |
| `addMaskMaterial(...)` + **正位移**（球或盒） | 2D / 3D | `-11` |
| 原始 `viennals` 掩膜 + 正位移（`getMaterialLevelSet` / `ps.ls.Domain(domain.getSurface())`） | 2D / 3D | `-11` |
| `applyToSingleMaterial(Si)` + 正位移 | 2D / 3D | `-11` |
| `domain.insertMask(...)` + 正位移 | 2D / 3D | `-11` |
| 掩膜盒**远离**表面（不重叠）+ 正位移 | 2D / 3D | `-11` |
| 掩膜盒**覆盖整个域** + 正位移 | 2D / 3D | `-11` |
| 沟槽（`MakeTrench` 掩膜）+ 正位移 | 2D / 3D | `-11` |
| `domain.insertMask(...)` + 负位移 | 2D | `0`，但表面完全不动（静默 no-op） |
| `domain.insertMask(...)` + 负位移 | 3D | `0`，只移动了约 0.05（掩膜被插到 level set 队首，语义与 `addMaskMaterial` 不同） |
| 负位移 + 掩膜覆盖整个暴露表面 | 2D / 3D | `0`，表面完全不动（合法的“全保护”，工作台拒绝记录，见 §8） |
| 掩膜材料不在域里（平面 + `addMaskMaterial(Mask)`） | 2D / 3D | `0`；原生日志 `Material Mask not found in domain.` / `None of the specified mask materials were found...`，退化为无掩膜 |
| 掩膜 level set 为空（0 点） | 2D / 3D | `0`；`GeometricProcessStrategy` 只在 `getNumberOfPoints() > 0` 时使用掩膜，退化为无掩膜 |
| `CustomSphereDistribution` ± 掩膜 | 2D / 3D | `-6`；`what(): Point ID 3 is out of bounds ...` |

结论：**正位移 + 非空掩膜 = 结构性崩溃**，不是掩膜几何或调用方式的问题
（近、远、全覆盖、材料掩膜、原始 `viennals` 掩膜、`applyToSingleMaterial`、
`insertMask` 全部 `-11`）。负位移 + 掩膜是官方支持路径，稳定可用。

### 3.3 4.7.0 / ViennaLS 5.10.0 交叉验证

用 `/tmp/opencode/vn470` 里的官方 wheel 建独立 venv 重跑同样的用例：

| 用例 | 4.6.2 / 5.8.5 | 4.7.0 / 5.10.0 |
| --- | --- | --- |
| 掩膜刻蚀 2D/3D | `0` | `0` |
| FinFET 掩膜刻蚀 | `0` | `0` |
| 掩膜沉积 2D/3D | `-11` | `-11` |

也就是说 4.7.0 没有修好这条路径，升级引擎不能解决；工作台不能把它包装成“可用”。

## 4. 根因

### 4.1 源码证据

ViennaLS `include/viennals/lsGeometricAdvect.hpp`（5.8.5 与 5.10.0 相同）在掩膜分支里
自己写着：

```cpp
// TODO: There are still issues with positive box distributions
// if there is a mask used!
// if point is part of the mask, keep smaller value
if (maskLevelSet != nullptr) {
  maskIt->goToIndicesSequential(currentIndex);
  // if dist is positive, flip logic of comparison
  if (distIsPositive ^ (std::abs(oldValue - maskIt->getValue()) < 1e-6)) { ... }
```

同一函数前面的“过滤贡献点”循环对掩膜迭代器做了 `isDefined()` 保护：

```cpp
if (!maskIt.isDefined() || !(maskIt.getValue() < *valueIt + 1e-5)) { ... }
```

而推进循环里的 `maskIt->getValue()` **没有** `isDefined()` 保护，且正位移时比较逻辑
被 `distIsPositive` 整体翻转。`setMaskLevelSet` 的文档也写明了隐式前提：
“This level set has to be wrapped by the levelset set by setLevelSet, so the mask is
entirely inside the advected level set.” —— 沉积是往外长，正好违反这个前提。

### 4.2 运行时证据

`gdb -batch -ex run -ex "bt 40"` 抓到的崩溃栈（wheel 已 strip，只有模块名）：

```text
Thread 1 "python" received signal SIGSEGV, Segmentation fault.
0x00007ffff388e307 in ?? () from .../viennaps/_core.cpython-312-x86_64-linux-gnu.so
#0  .../_core...so
#1  .../_core...so
#2  .../_core...so
#3  GOMP_parallel () from .../viennals.libs/libgomp-...so.1.0.0
#4  .../_core...so
...
```

崩溃发生在 `GeometricAdvect::apply()` 的 OpenMP 并行区内（`GOMP_parallel` 之上就是
`_core` 帧），也就是上面那段掩膜比较代码附近；不在 Python 绑定、不在 `FromMesh`、
不在进程收尾。

### 4.3 最强假设

正位移时掩膜迭代器会被推进到掩膜窄带（`Expand(mask, 3)`）之外，而推进循环里的
`maskIt->getValue()` 没有 `isDefined()` 保护，读到未定义/越界的 run 值；负位移时表面
只在掩膜内部收缩，不会离开窄带，所以只有正位移崩。上游 TODO 与“掩膜必须被表面包裹”
的文档前提都指向同一处。没有符号表，无法定位到具体行号，因此这是**假设**，不是已证结论；
但“正位移 + 任意非空掩膜必崩、负位移稳定”这一行为在 5.8.5 和 5.10.0 上可重复。

**因此这不是我们的掩膜构造问题**：官方示例的掩膜、`getMaterialLevelSet` 原始掩膜、
`insertMask`、`insertNextLevelSetAsMaterial` 都试过，正位移一律崩；负位移一律正常。
仍然成立的可控项是“我们只能使用负位移 + 掩膜”。

## 5. 探测命令

```bash
cd /root/wb-a4-mask
mkdir -p var/probe-a4-mask

# 单用例：3D、沟槽、球、半径 -0.1（刻蚀）、材料掩膜
printf '%s' '{"dimension":3,"geometry":"trench","shape":"sphere","magnitude":-0.1,"mask":"material","maskMaterial":"Mask","process":"two-arg"}' \
  > var/probe-a4-mask/etch-3d.case.json
.venv/bin/python scripts/bounded_child.py --cwd "$PWD" \
  --record var/probe-a4-mask/etch-3d.guard.json \
  --log-path var/probe-a4-mask/etch-3d.log \
  --timeout-seconds 60 --max-rss-mib 4096 --max-log-bytes 1048576 -- \
  .venv/bin/python worker/probe_particle_mask.py \
  var/probe-a4-mask/etch-3d.case.json var/probe-a4-mask/etch-3d.out.json
echo "rc=$?"; cat var/probe-a4-mask/etch-3d.guard.json
```

整张矩阵一次跑完（每个用例仍是独立 bounded child）：

```bash
.venv/bin/python worker/run_particle_mask_probe.py   # 结果写 var/probe-a4-mask/summary.json
```

`probe_particle_mask.py` 的 `case.json` 字段：`dimension`（2/3）、`geometry`
（`plane`/`trench`/`stack`/`mask-box`/`finfet`/`mask-empty`/`mask-full`）、`shape`
（`box`/`sphere`/`customSphere`）、`magnitude`（带符号）、`halfAxes`、`radii`、
`mask`（`none`/`material`/`getMaterialLevelSet`/`applyToSingleMaterial`/
`surface-domain`）、`maskMaterial` 或 `maskMaterials`、`targetMaterial`、
`maskBox`（覆盖掩膜盒坐标）、`insertMode`（`material`/`mask`）、`process`
（`two-arg`/`three-arg`）、`duration`、`boundary`、`debug`。探针在调用前就把域状态
写进输出文件，所以崩溃后仍能读到 `diagnostics.before`。

`worker/probe_particle_distributions.py` 保留为无掩膜 / `CustomSphereDistribution`
的最小探针，`tests/test_particle_shape.py` 里的历史崩溃证据仍用它复跑。

## 6. 探测结果（无掩膜部分）

| 用例 | 维度 | 结果 |
| --- | --- | --- |
| `BoxDistribution` +0.2，无掩膜，平面 | 2D / 3D | `0`；z：`0 → +0.2` |
| `SphereDistribution` +0.2，无掩膜，平面 | 2D / 3D | `0`；z：`0 → +0.2` |
| 负参数 −0.2，无掩膜，平面 | 2D / 3D | `0`；z：`0 → −0.2` |
| 无掩膜，沟槽（`MakeTrench`） | 2D / 3D | `0`；顶面 0.2 → 0.3，沟底 −0.425 → −0.35（球，0.1） |
| 无掩膜，三层叠层 | 2D / 3D | `0`；只有顶层界面移动 |
| `duration` 0 / 0.05 / 1 / 5 | 2D / 3D | 结果逐位相同 → 原生忽略持续时间 |
| 半轴/半径 = 0 | 2D / 3D | `-11`；日志先出现 `One half-axis ... smaller than the grid Delta!` |
| 半轴 `< gridDelta`（0.01） | 2D / 3D | `0`，但原生日志警告分布可能被破坏 → 工作台在校验阶段拒绝 |
| 混合符号 `[0.1,−0.2,0.3]` | 2D | `0` 但表面被清空（快照无点），属于不可用结果 |
| 混合符号 `[0.1,−0.2,0.3]` | 3D | `-11` |
| 周期性边界，平面 | 2D / 3D | `0`、平面位移正确；ViennaLS 的 `lsGeometricAdvect.hpp` 明确标注 periodic 仍是 TODO → 工作台拒绝 |

崩溃日志（`var/probe-a4-mask/*.log`，不提交）：

```text
# 掩膜沉积
    Applying geometric model...
# 随后进程被 SIGSEGV 结束（guard.returnCode = -11）

# CustomSphereDistribution
    Applying geometric model...
terminate called after throwing an instance of 'std::runtime_error'
  what(): Point ID 3 is out of bounds for CustomSphereDistribution with 3 radii!
# guard.returnCode = -6
```

## 7. JSON 合同

`particleShape` 是**几何步骤**，不是 `Process` 步骤：没有 `duration`、没有速率、
没有随机种子/通量引擎，也不写 `actualRate`。它只出现在 schema 7 的步骤联合类型里。
不需要新端点和上传资产：项目继续通过既有的 `/api/projects`、`/api/runs`、
`/api/runs/{runId}/replay-project` 与 `/api/runs/replay` 传递；旧项目与该字段缺席时
的请求哈希完全不变。

```json
{
  "id": "step_shape_0001",
  "name": "表面圆化",
  "enabled": true,
  "operation": "deposit",
  "model": "particleShape",
  "shape": "sphere",
  "radius": 0.5
}
```

```json
{
  "id": "step_shape_0002",
  "name": "盒形收缩",
  "enabled": true,
  "operation": "etch",
  "model": "particleShape",
  "shape": "box",
  "halfAxes": [0.5, 0.25, 0.75]
}
```

字段与校验：

| 字段 | 规则 |
| --- | --- |
| `operation` | `deposit`（正位移）或 `etch`（负位移）；`halfAxes`/`radius` 永远是正数 |
| `shape` | `sphere` 或 `box` |
| `radius` | `shape=sphere` 必填，`> 0` 且 `≤ 2 µm`，并且 `≥ domain.gridDelta` |
| `halfAxes` | `shape=box` 必填；2D 恰好 2 个 `[x, h]`，3D 恰好 3 个 `[x, y, z]`；每项 `> 0`、`≤ 2 µm`、`≥ gridDelta` |
| 掩膜 | **没有步骤字段**：掩膜来自项目几何的原生掩膜材料。`mask`、`maskMaterials` 等额外字段一律 422（`extra=forbid`） |
| 持续时间/速率 | **不可表达**：`duration`、`rateMagnitude` 等额外字段 422 |
| 维度 | 2D/3D 都支持；`halfAxes` 长度不符合维度时在 ProjectV7 校验中 422 |
| 边界 | 只允许 `reflective`；`periodic` 在 `validate_project` 中 422 |
| Python 回调 | 不可表达（没有 callable/module 字段） |

工作台校验还会拒绝 `0` 和小于一个网格间距的值：原生在 0 时崩溃，在
`< gridDelta` 时打印“distribution may break”的警告。混合符号无法表达，
符号统一由 `operation` 决定。

## 8. 工作台执行与诊断

`worker/simulate.py::apply_particle_shape` 构造真实原生对象：

```python
model = ps.BoxDistribution([sx, sy, sz])       # 2D 传 [sx, sy, 0.0]
model = ps.SphereDistribution(sign * radius)
for name in masked_materials:                  # 只有 etch 且材料 level set 非空
    model.addMaskMaterial(material_handle(ps, name))
process = ps.Process(domain, model, 0.0)       # duration 原生不参与，固定记录 0.0
process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
process.apply()
```

`CPU_DISK` 必须显式固定：`Process` 默认是 `AUTO`，即使是纯几何推进也会解析通量引擎并
初始化 CUDA（`cuInit`）。实测把 `CUDA_VISIBLE_DEVICES` 置空后，修复前的最小平面刻蚀
直接失败（`Cuda Error CUDA_ERROR_NO_DEVICE` / `Failed to load CUDA symbol via
cuGetProcAddress: cuInit`），修复后同样的 CPU 运行正常完成。参考 fixture 与两个探针
也固定同一后端，避免参考实现和生产路径走不同的引擎。

掩膜材料来自 `worker/mask_materials.py::protected_material_names(project, step)`
（与通量/方向性/CSV 步骤同一份规则）：

- `trench`/`hole`/`fin` 且 `maskHeight > 0` → `maskMaterial`；
- `stack` → `role=mask` 的图层（以及 `gdsMask.protectsEtch` 的材料）；
- `gds` → `mask=true` 的图层；
- `plane` → 空。

`etch` 且域里确实存在该材料的 level set 时才调用 `addMaskMaterial`；`deposit`
永远不调用（原生必崩）。运行记录写入 `manifest.actualExecution[].actualParameters`：

- `geometryOperation: "particleShape"`、`nativeClass`、`nativeConstructor`、
  `nativeMaskConstructor`（`addMaskMaterial` 或 `null`）；
- `dimension`、`operation`、`shape`、`radius` / `halfAxes`（请求值）；
- `nativeSignedRadiusUm` / `nativeSignedHalfAxesUm` / `nativeHalfAxesUm`（实际传入值）；
- `expectedOffsetUm`（球半径或盒最大半轴）；
- `surfaceBoundsBeforeUm` / `surfaceBoundsAfterUm`（真实表面网格包围盒）；
- `surfaceSignatureBefore` / `surfaceSignatureAfter`（表面网格 SHA-256：全部节点坐标、
  line/triangle 拓扑、逐 cell 的 `MaterialIds`、域材料映射与标量标签）；
- `surfacePointCountBefore/After`、`surfaceCellCountBefore/After`；
- `nativeProcessBackend`：固定为
  `CPU_DISK pinned; geometric distribution has no stochastic flux engine`；
- `levelSetPointCountsBefore` / `After`、`materialIdsByLevelSetIndex`；
- `nativeMask`：
  - `mode`：`material` 或 `none`；
  - `applied`：是否真的调用了 `addMaskMaterial`；
  - `materials`：实际传入的材料名；
  - `omittedMaterials`：几何声明了但没有传入的材料（沉积一律在这里；
    刻蚀时材料不在域里也会出现在这里）；
  - `reason`：为什么这样构造（刻蚀保护 / 沉积必崩 / 无掩膜）；
- `nativeProcessDurationSeconds: 0.0`、`nativeDurationIgnored`；
- `scopedSurface`：沉积只推进顶层材料，刻蚀会与下层 level set 求交。

worker 在调用前后各取一次表面网格签名；只有签名完全相同才算 no-op，否则抛错而不是记录
一个静默 no-op（错误信息里会带上生效的掩膜材料名，因为“掩膜覆盖整个暴露表面”正是
一种合法的原生 no-op）。

**不能只用包围盒判断**：带掩膜的球刻蚀作用在图案化叠层上时，被保护的掩膜顶面与衬底
底面把每个轴的包围盒都钉住，而开口底部的表面仍在移动。实测同一工程的包围盒逐位
相同，但表面节点 2D `48 → 50`、3D `816 → 850`，cell 数 `46 → 48`、`1472 → 1536`；
只比较包围盒会把这步真实刻蚀误判成 no-op 并失败。签名覆盖坐标、拓扑与材料，因此
真实变化一定被发现，而完全没变（例如顶层 level set 本身就是被保护的掩膜）仍然判为
no-op。`actualEngine` 固定为 `not-applicable`，`actualSeed` 为 `null`。

面板/校验提示（`backend/validation.py::_particle_shape_warnings`）按操作分别说明：
刻蚀保护原生掩膜材料；沉积因为原生缺陷始终无掩膜、项目掩膜材料也会一起长大。

## 9. 测试

`tests/test_particle_shape.py`：

1. 2D/3D 球沉积、盒刻蚀与独立原生参考
   （`tests/fixtures/particle_shape_direct_reference.py`）比较坐标（≤ `1e-6`）、
   拓扑、`MaterialIds`、`materialIdsByLevelSetIndex`；
2. 2D 沟槽上的盒沉积与原生参考比较，覆盖多 level set，并断言沉积把 `Mask`
   记为 `omittedMaterials`；
3. **2D/3D 沟槽上的球刻蚀与原生参考比较**，断言 `nativeMask.applied`、
   `materials == ["Mask"]`、`nativeMaskConstructor == "addMaskMaterial"`；
4. **定位性**：同一条原生刻蚀，带掩膜与不带掩膜的两个参考快照相比，掩膜顶面
   保持 `maskHeight`、无掩膜参考低 0.2 以上，坐标最大偏差 > 0.2；
5. **自定义材料注册表**：`POST /api/materials` 注册的 `WB_A4_Mask` 用作
   `geometry.maskMaterial`，刻蚀运行记录写 `materials == ["WB_A4_Mask"]`，重放逐位一致；
6. 连续两个形状步骤 + replay 逐位一致；
7. **图案化叠层上的掩膜球刻蚀**（2D/3D）：包围盒逐位相同但节点/cell 数增加、签名改变，
   步骤必须成功并写出可重放的快照——这是“包围盒误判 no-op”的回归；
8. **真 no-op 仍然失败**：顶层 level set 就是被保护的掩膜时签名不变，步骤必须报
   `left the surface unchanged`；
9. **CPU-only 执行**：`CUDA_VISIBLE_DEVICES` 置空时，无掩膜平面刻蚀与带掩膜沟槽刻蚀
   都必须成功，记录 `nativeProcessBackend = CPU_DISK pinned...`，且 `run.log` 中没有
   `Cuda Error` / `cuGetProcAddress`；
10. 掩膜/周期边界/零半径/低于网格间距/错误维度/额外字段全部 422；
11. 既有项目请求哈希固定在基线值 `605a109b...c42`，新增步骤只在启用时改变哈希；
12. 项目保存/重开保持字段；
13. bounded child 复跑的实测证据：
    - `CustomSphereDistribution` 2D/3D `-6`；
    - 掩膜沉积 `-11`（材料掩膜、原始 `viennals` 掩膜、远离表面的掩膜盒、覆盖全
      域的掩膜盒）；
    - 零半轴 `-11`；
    - **掩膜刻蚀 2D/3D `0`，掩膜顶面不动、衬底下降**（`probe_particle_mask.py`，
      探针记录 `processBackend: CPU_DISK`）。

## 10. 仍未覆盖 / 不声称

- 不暴露**掩膜沉积**：原生缺陷，工作台只能拒绝并记录，不能用“先加掩膜以后再修”绕过。
- 不暴露 `CustomSphereDistribution`：需要与表面点数一一对应的半径数组，
  不能从项目几何稳定推导。
- 周期性边界没有暴露：原生几何推进的 periodic 支持仍是 TODO。
- 掩膜刻蚀的掩膜材料由项目几何决定，步骤本身不能单独勾选“保护哪些材料”；
  这与工作台其它步骤一致，但和不带掩膜的旧行为相比，`trench`/`stack`/`gds`
  工程上的 `etch` 结果会变（掩膜材料不再被刻蚀）。
- 掩膜覆盖整个暴露表面时原生会静默不动，工作台因此直接失败，而不是产出一份
  “没有变化”的快照。
- 这是形态学几何偏移，不是物理沉积/刻蚀：没有通量、黏附概率、角度分布、
  材料选择比或时间演化；位移是参数值，不是 `rate × duration`。

## 11. 与 isotropic 分流的关系

`model: "isotropic"` 的 2D `auto` 复用同一原生 `SphereDistribution`（无掩膜沉积 /
带掩膜刻蚀）做一次均匀半径的几何偏移；3D `auto` 始终走 level-set
`IsotropicProcess`，因为本仓库测得的 3D 几何路径更慢。`particleShape` 仍是显式的
几何步骤，不参与 `auto` 分流，也没有速度/材料速率表；两者的字段、安全边界和记录
不要混用。分流规则见 [`isotropic-dispatch-contract.md`](./isotropic-dispatch-contract.md)。
