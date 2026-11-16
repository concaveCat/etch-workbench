# 引擎抽象与双引擎对比（Phase A：M0 / M1 / M1.5）

> 状态：M0 + M1 + M1.5 + M2 已落地——协议、调度器、两个适配器、公共探针格、截面指标、
> 各向异性、CMP 判定、成本守卫、checkpoint、对比 API、三页前端、测试。
> 本文只描述已经能跑的东西，以及明确**还没做**的东西（掩膜/GDS，见 §5.6）。

## 1. 目标与边界

同一份编译好的流程，在两个引擎上各跑一遍，逐步对齐、逐步计时、左右对照。
本轮只做**两端都支持的几何子集**：

| step | ViennaPS 侧 | SPX 侧 | 可比性 |
|---|---|---|---|
| 初始结构（平面 + 薄膜栈） | `MakePlane` | `substrate` + `depo_isotropic` | exact |
| 各向同性刻蚀/沉积 | `IsotropicProcess` + `setMaterialRate` | `etch_isotropic` / `depo_isotropic` | exact |
| 各向异性刻蚀/沉积 | `DirectionalProcess` | `etch_anisotropic`（Angle）/ `depo_anisotropic` | approximate |
| 平坦化 | `Planarize(height)` | `etch_cmp`（offset 语义未映射） | **no_counterpart** |

明确**不做**：等离子体/射线追踪模型（sf6o2、singleParticle…）、ALD、oxidation、
neutralTransport。它们在 SPX 侧没有对应方程；调度器把这类步骤标成
`unsupported`，绝不用另一个引擎的结果替代。

## 2. 三个契约

### 2.1 `StepIR`（`engines/protocol.py`）

引擎中立的步骤描述，也是编译器唯一该产出的东西。

```json
{"step_id": "s1_etch", "kind": "etch", "mechanism": "isotropic",
 "duration": 0.05, "rates": {"SiO2": -1.0, "Si": -0.02},
 "anisotropy": 1.0, "comparability": "exact"}
```

单位约定（写死在协议里，不由流程决定）：长度 µm、时间 min、z 向上、
原点在衬底上表面中心、去除为负。PM 之外的一切（schema 版本、项目单位换算）
留在编译器里。

### 2.2 Engine SPI（子进程 + 作业协议）

```
job.json ──► engines/<adapter> ──► <out_dir>/result.json + frames/*
```

- `engines/viennaps/engine.py`：workbench venv，复用
  `process_models.v2.engines.viennaps.material_handle`，不重复材料目录知识。
- `engines/spx/bridge.py`：SPX 私有 Python 3.11，`init_session_licenses` →
  `import spx.procedural`（顺序不能反，见 §4）。

两个引擎都是子进程，因为 SPX 链接自己的 libpython 与 .so，无法与 workbench venv 同进程。

### 2.3 `Frame`（渲染契约 v2）

```json
{"step_id": "...", "kind": "mesh",   "path": "frames/s1-viennaps.mesh.json", ...}
{"step_id": "...", "kind": "raster", "path": "frames/s1-spx.png", ...}
```

`mesh` 帧是 `{points, triangles, materialIds, materials}`；`raster` 帧是 PNG +
固定相机说明。旧的每步快照（`var/runs/<id>/<step>.json`）完全不受影响。

## 3. 调度器（`engines/scheduler.py`）

| 行为 | 实现 |
|---|---|
| 引擎并行 | 每个引擎一个子进程，`ThreadPoolExecutor` 并发 |
| 对齐 | 严格按 `step_id`；缺失记 `missing`，失败记 `failed`，**不插值、不替换** |
| 可比性 | `engines/comparability.json` 按引擎对声明，写进 `compare.json` |
| 产物 | `out/compare.json` + `out/engines/<id>/frames/*` |
| 幂等 | 同一 `StepIR[]` → 同一 `jobDigest` |
| 失败 | 不重试到别的引擎；`status` 由引擎自报 |

## 4. 实测到的 SPX 事实（都写进代码注释了）

1. `spxwebapi` 自己不初始化 session licence，WebSocket 一连就
   `License check failed`。进程内必须先 `init_session_licenses(LicenseFeature.all, N)`。
2. 官方示例 `examples/Memory1_Route.tbson`：**88 步、3.3 s 跑完**，`meas_screenshot`
   单帧 ~0.5 s。
3. `spx.run()` 一次跑完整条流程后，可以按 step 逐个取截图（用缓存的 clipstate），
   不需要逐步重跑——M0 就是靠这条。
4. 一条 route 必须带 clip，clip 必须引用 layout，且 **clip 窗口必须落在 layout 覆盖范围内**，
   否则 `spx.run()` 抛空消息。Phase A 复用 `Logic1_Layout.oasis` 的
   `[0.035, 0, 0.175, 0.288] µm` 窗口。
5. 从 `.tbson` 载入 route 时必须传
   `MergeMaterials=take_hosted_materials(..., ns)`，且 `ns` 必须是**自己的**命名空间
   （`spx.procedural` 的全局库是另一个）。
6. `substrate.ColumnSizeX/Y` 是周期单元，不是绘制范围；结构范围由 clip 决定
   （用 `spxwebapi.utils._get_bounding_box` 实测确认）。

## 5. 逐点比对：公共探针格（M1）

两个引擎的几何表示不同（ViennaPS 是水平集+网格，SPX 是布局上的 CSG），
所以"逐点"只能在**同一组采样点**上比材料占用标签。

**调度器负责造格**（`engines/probe.py`），写进 `job.probe`，两个引擎回答同一批点：

- 默认 3000 点（`--probe-points` 可调，0 = 关闭），格点取半格偏移，
  避免正好落在界面上（两侧对"界面上的点"定义不同）；
- 覆盖 `x∈[0,width]`、`y∈[0,depth]`、`z∈[-substrate_thickness, height]`。

**两端的采样方式（写进 `compare.json.probeMethod`）**

| 引擎 | 方法 | 说明 |
|---|---|---|
| SPX | `meas_probe` | `measure(Type='meas_probe', Height=(z,um), PointName=['','',[x,y]], GroupBy='region')` → 材料名；实测 ~1.6 ms/点，**不支持批量**（PointName 只吃一个 3 元组） |
| ViennaPS | `height_column` | 从表面网格按列取每材料最高 z，点属于"高于该点且最低的那个 top" |

ViennaPS 侧为什么不用 `DenseCellSet`：在 0.002 µm 的域上它需要 **1481 万个格子、107 秒**，
不可用。`height_column` 对 z 单调的层栈（平面、各向同性刻蚀/沉积、CMP）是精确的，
对凹形结构不成立——所以方法名写进结果，不用一个数字假装通用。

**指标**（`engines/probe.py`）：`agreement`（同标签点占比）、每材料 `iou`、
`mismatchCount` + 前 5 个不一致点、两边的每材料 `topHeightUm`。
长度不一致、缺一方结果都显式报 `invalid` / `incomplete`，不会返回 0。

## 5.1 坐标归一化（不做这个，逐点比全是噪声）

SPX 的结构画在 layout 的窗口上（例如 `[0.035, 0, 0.175, 0.288] µm`），
ViennaPS 的域从 0 开始。协议里 `domain.origin_xy` 声明窗口的全局左下角：
`StepIR` 坐标始终是**结构局部**坐标，SPX 适配器在探测时加这个偏移。
第一次跑出来 86% 一致率就是因为没做这一步（所有 `x<0.035` 的点在 SPX 侧都是空的）。

材料名同理：SPX 叫 `Silicon`/`Oxide`，空处报 `Gas`/`None`；统一映射在
`engines/normalize.py`（表在 `normalize.json`），void 别名收成一个 `void`。
没有这层时两边的"一致率"是 0，而两个引擎都是对的。

## 5.2 截面指标（M1.5）

SPX 能导出结构，但都不好用：`meas_save_3D` 的 `Format` 实测只能是
`{ gcns, native, stl_3dprint, tdr }`，其中 `native`/`tdr` 写出来是 **HDF5**
（`\x89HDF` 头），`gcns` 只有 227 字节（结构块引用），`stl_3dprint` 还要额外的
`Representation` 参数。workbench 虚拟环境没有 HDF5 读取器，为一个指标引入二进制
依赖不划算。

所以截面指标改成"问高度"而不是"读结构文件"：

- 调度器给定 33 个列（`y = depth/2`，x 均布）与材料清单；
- **ViennaPS** 直接由表面网格的每列材料顶高回答（精确）；
- **SPX** 用 `meas_probe` 二分：25 点粗梯找区间，再 12 次二分，
  分辨率 `span/2^12 ≈ 0.17 nm`；实测每 probe ~1.3 ms，33 列 × 2 材料 ≈ 5 s；
- 指标：每材料 `meanAbsUm` / `maxAbsUm` / `L2`；未解析的列计入 `missingColumns`，
  不当 0。

实测（平面 Si + 100 nm SiO₂，各向同性刻蚀 0.05 min）：

| 步 | Si 平均 \|Δz\| | SiO₂ 平均 \|Δz\| |
|---|---|---|
| s0_init | 4e-6 µm | 1e-6 µm |
| s1_etch | 4e-6 µm | 8.3e-5 µm（0.083 nm） |

**相机**：`meas_screenshot(View=…)` 只接受
`{ free, iso, iso90, iso180, iso270, xy, xz, yx, yz, zx, zy }`。

`free` 就是**自由相机**，且能无 GUI 用——实测要点（`engines/camera.py`）：

| 事实 | 说明 |
|---|---|
| 参数形状 | `CameraFocalPosition` / `CameraPosition` 必须是 `([x, y, z], unit)` 的 quantity；裸 `[x,y,z]` 报 `Object of type 'float' is not an instance of 'sequence'` |
| 正交投影 | `OrthographicProjection=True` 可用，正好对上盘面/网格的正交视图 |
| 退化情形 | 相机位置正好在焦点正上方（仰角 90°）→ `Boost QVM error`，所以 `abs(elevation) > 85` 时退回预设 |
| 有效性 | 换 `CameraPosition` 渲染结果字节不同（同 `focal` 下 (1,1,1) 与 (1,0,0) 哈希不同），说明方向真的生效 |

映射：焦点 = 结构 bbox 中心（含 origin 偏移），位置 = 焦点 + 方向 × `4·extent/zoom`，
`extent = max(width, depth, z_high − z_low)`。UI 里"自由相机 / 预设"二选一，
左侧网格用同一方位投影（`cameraForView`），改视角需要重新运行。

`viewsnapshot` 命令确实存在（路由级节点），但它只暴露光照/背景/`OrthographicProjection`/
`CameraViewAngle` 等渲染设置，**没有** azimuth/elevation/position；`meas_screenshot(View=…)`
也不接受快照名。所以自由相机只能走上面这条 `free` + 两个 quantity 的路。

## 5.3 各向异性与 CMP（M2）

**各向异性**：两边都支持，但模型不同，所以等级是 `approximate`。

| | 映射 |
|---|---|
| ViennaPS | `StepIR.anisotropy` 把每个速率拆成 `(directional, isotropic) = (r·a, r·(1−a))`，喂给 `DirectionalProcess(direction, {material: (dir, iso)}, 0, 0)` |
| SPX | `etch_anisotropic(MaterialDependent=…, Angle=(90·a, deg), Time=…)`；`depo_anisotropic` 只有 `MaterialFilter`+`Rate`+`Time`，没有角度/指数 |

实测（`engines/examples/m2-aniso-cmp.json`，a=0.8）：逐点一致率 100%，
截面 Si 6e-6 µm。

**CMP：判定为 `no_counterpart`，不是"差不多"。** 实测 SPX 的 `etch_cmp`：

| 调用 | 结果 |
|---|---|
| `EtchstopMaterialsFilter=Oxide, Offset=0` | 0.1 µm 氧化层被**整层去掉**（顶部落到 z=0） |
| 同上 `Offset=-0.03` | 结果不符合"相对停止面"的直觉 |
| `EtchstopMaterialsFilter=[[]], Offset=0.02` | **无变化**（0.05 仍是 0.05） |

也就是说 SPX 的 offset 不是"绝对高度"，也不是"从顶面往下磨多少"。
没搞清之前不假装可比：SPX 适配器对该步显式返回
`unsupported`（"SPX etch_cmp offset semantics are not mapped to an absolute cutoff"），
`comparability.json` 里 `planarize/cmp` 记 `no_counterpart`。ViennaPS 侧
`Planarize(domain, cutoff)` 正常执行。

## 5.4 成本估算与域守卫（M2）

`engines/limits.py` 对每个步骤给两个数（纯计算，不碰几何）：

```
activePoints = (width/Δx)·(depth/Δx)
ratio        = max|rate|·duration / Δx        # 位移折合格点数
```

每个引擎在 `registry.py` 里声明 `limits`（viennaps：5e6 点 / 200Δx；spx：1e7 / 2000Δx）。
超限的引擎对该步报 `out_of_domain`，**并且不再推进**：后续步骤一律
`out_of_domain`（"skipped: an earlier step … left this engine's domain"），
绝不在"少执行了一步"的几何上继续出帧。

实测（`engines/examples/m2-out-of-domain.json`，5.0 µm 位移 = 2500Δx）：

```
s1_deep   viennaps: out_of_domain  "displacement 5.0 um is 2500.0 grid cells, above the viennaps limit 200.0"
          spx:      out_of_domain  "… above the spx limit 2000.0"
s2_after  viennaps: out_of_domain  "skipped: an earlier step (s1_deep) left this engine's domain"
```

`compare.json` 的每一步都带 `estimate`（`activePoints` / `displacementUm` / `ratio`），
UI 与后续成本模型直接读它。

## 5.5 checkpoint / restore（M2）

- ViennaPS：每步写 `out/state/<step>.vpsd`（`ps.Writer`），`StepOutcome.checkpoint`
  记录相对路径；`JobSpec.restore` 指定一个 checkpoint 时，引擎先建同样的 domain，
  再用 `ps.Reader` 载入并跳过初始结构构建。
- 实测：从 `s1_aniso.vpsd` 恢复后 SiO₂ 顶高 0.05 µm（与原始 aniso 步一致），
  再执行 CMP 得到 0.02 µm。
- SPX：没有 checkpoint（route 状态不可序列化到我们的格式），保持不声明该能力。
  跨引擎热换仍是一个"要重新播放几何"的问题，不在本轮范围。

## 5.6 掩膜 / GDS 与复杂图形（M3：SPX 侧已可用，ViennaPS 侧待接）

**先纠正上一版的结论**：`layout_extrude_structure` 不是 SPX 唯一的掩膜机制，而且
真正用来做复杂图形的是另一个命令。

### SPX 怎么做出 Memory1 这种结构

Memory1 的每一步都能在 `spx.io.node_to_python` 导出的脚本里读到，图形来自两件事：

1. **`litho_patterning(LayerNames=[…], Sigma=(σ,um))`** —— 布局驱动的光刻步，
   把 layout 的某一层变成光刻胶图形。Memory1 用到 6 个层名：
   `PLG`（插塞）、`CS`（缝）、`PC`（接触）、`M1`（位线 strap）、`BC`（位线接触）、`M2`（位线）。
2. **几何步**：`depo_fill`（把凹陷填平）、`etch_anisotropic`、`etch_isotropic`、
   `etch_ellipsoidal`（带侧向速率的锥形刻蚀）、`etch_strip`、`etch_cmp`。

必需的前置（少一个就退化成平面柱体）：

```python
sp.layout(Layout=<layout 路径>, Path=<layout 路径>)        # 打开 layout
sp.route(File='t', Route='Memory1', MergeMaterials=sm.take_hosted_materials)  # 材料库
su.update_clip_layout({'Route': 'Memory1'}, <layout 路径>)  # clip 指向该 layout
```

实测：Memory1 全流程 **88 步、5.5 s**，渲染出带插塞/位线的真实三维结构
（`/tmp/mem1_last.png`：分层 + 竖直线 + 顶部条状位线图形）。不加 layout 时是平面柱体 ——
这就是上一版"无法做出图案"的真实原因，不是命令不可用。

### 本次额外验证

| 实验 | 结果 |
|---|---|
| `litho_patterning(LayerNames=['PLG'])` 打在 Memory1 layout 上，再各向异性刻蚀 | **出来插塞阵列**（光刻胶上的孔阵列，刻穿到氧化层）——SPX 掩膜通路可用 |
| 同上换 `M2` | 另一种图形（哈希不同） |
| 用工作台自带的 `tests/fixtures/finfet22_workbench.gds` + `LayerNames=['1/0'|'2/0'|'1'|'0/0']` | 四者渲染完全相同，且都是**整片光刻胶**（无图形）——GDS 的层名没被校验/没匹配上，窗口 [0,0.5]² µm 也可能落在单个大多边形内部 |
| `layout_extrude_structure` | 需要上一步的 `in_layout_state`，且层名在 headless 下取不到（见上一版记录） |

### 为什么 Memory1 不能直接当"两边都做"的验收

| Memory1 用的 | ViennaPS 有吗 |
|---|---|
| `litho_patterning` | 没有；ViennaPS 的掩膜是"保护材料"（`maskMaterials`），图形必须由外部几何给出 |
| `depo_fill` | 没有（只有保形沉积 + `Planarize`） |
| `etch_ellipsoidal` | 没有（`DirectionalProcess` 是方向+各向同性两分量） |
| `etch_strip` | 等价于"去掉某材料"，可以用几何步近似，但不是原生模型 |
| layout 是 `.oasis` | `backend/gds.py` 只读 GDS |

### 可行的验收路线（建议）

共用几何基座 = **层栈 + 一层掩膜**，掩膜图形来自**同一个 layout 文件**：

1. SPX 侧已经能做：`litho_patterning(LayerNames=[…])` + layout（实测出插塞阵列）。
2. ViennaPS 侧要补：GDS 多边形 → 掩膜材料（工作台已有 2D GDS 掩膜实现
   `backend/gds.py` + `worker/gds_mask.py`，缺的是 `StepIR(kind='mask')` 适配）。
3. 两边读同一份 GDS：SPX 读 GDS 的层名规则待定（见上表），一次性把 `.oasis`
   转成 `.gds`（外部工具，一次性，不进运行时依赖）比在 drift 中猜层名可靠。
4. 验收用例建议取 Memory1 的 **Plug_Patterning 模块**（depo + litho + 各向异性刻蚀 + strip），
   它只用到"层栈 + 一层掩膜 + 各向异性"，而不是整条 88 步流程；
   `depo_fill`/`etch_strip`/`etch_ellipsoidal` 按实测分别标 `approximate` / `no_counterpart`。

到目前为止 `mask/gds_mask` 仍记 `no_counterpart`（ViennaPS 没接），但 SPX 侧不再是未知数。

## 5.7 安全护栏（事故记录）

SPX 没有任何规模保护：它把整个 clip 窗口的结构一次性建进内存。
一次手测里把窗口从 `0.624 x 0.740 µm` 改成 **`700 x 700 µm`**（为了查 GDS 单位），
WSL 直接重启。

现在的三道护栏：

| 位置 | 作用 |
|---|---|
| `limits.window_verdict` | 窗口面积上限（spx 1.0 µm² / viennaps 100 µm²），**启动引擎之前**拒绝 |
| `scheduler._address_space_guard` | SPX 子进程 `RLIMIT_AS=6144 MiB`（`VIENNAPS_WORKBENCH_SPX_MEM_MIB` 可调） |
| `engines/spx/bridge.py` | 只跑小窗口，手测也必须走 bridge / `engines.cli` |

已知安全规模（实测）：`0.1 x 0.3 µm` 几秒；`0.624 x 0.740 µm`（Memory1 clip）88 步 5.5 s。

## 5.8 Layout 读取（M3，第 1 步已通）

`engines/layout.py` 不启动任何引擎，只回答一个布局文件里"有什么"：

| 输入 | 读取方式 |
|---|---|
| GDS | 自己写的 GDSII 记录读取器（`read_gds_polygons`，含 GDS 的 base-16 实数解码），不依赖 backend/numpy，可在 SPX 解释器里 import |
| OASIS | `gdstk`（一个纯 Python/C 轮子，非 Synopsys 代码）；`to_gds()` 用**显式**单位 `unit=1e-6 m / precision=1e-9 m` 转 GDS |

实测（Memory1 自带 layout）：

```
OASIS 单元 a，六层 9/11/15/17/19/21，extent 0×0 → 0.624×0.740 µm
转 GDS（1 µm/1 nm）后：层号、多边形数、每层包围盒、整体 extent 全部逐值相等
层号    多边形  包围盒 (µm)
 9       18     (0, 0, 0.624, 0.740)
11       18     (0, 0.149, 0.624, 0.591)
15       30     (0, 0, 0.624, 0.740)
17       16     (0, 0, 0.624, 0.740)
19       18     (0, 0.161, 0.624, 0.579)
21       16     (0.01, 0, 0.614, 0.740)
```

也就是说 **SPX 用层名、我们用层号**，两边看的是同一份几何；OASIS 不需要写解析器，
只需要一个可选依赖 `gdstk`（未安装时 `LayoutError` 明说，不静默降级）。

一个**负结果**（记下来避免重复踩）：`litho_patterning(LayerNames=[...])` 对六个层名
各渲染一张，**六张互不相同**（说明 SPX 确实按名字解析），但在"先整片沉积光刻胶、
再 litho"的流程里，探针在 z=0.11…0.18 全部报 Photoresist——说明这个写法下 litho
**没有真的开孔**。Memory1 里的顺序是 `depo_fill BARC/SiOC → litho → etch_aniso → strip`，
极性/厚度契约得从能跑的模块里读，不能靠猜。

## 5.9 四个"SPX 独有"能力已补齐（M3）

上一版列的四个缺口现在两边都能跑，同一个 `StepIR` 序列、同一次对比运行：

| StepIR | ViennaPS 侧实现 | SPX 侧 | 等级 | 实测 |
|---|---|---|---|---|
| `kind=strip, mechanism=strip` | `domain.removeMaterial` | `etch_strip(MaterialFilter)` | exact | 一致率 1.00 |
| `kind=deposit, mechanism=fill` | `Box` LS 减去所有已有实体 → `insertNextLevelSetAsMaterial` | `depo_fill(Offset, ReferenceMaterialsFilter)` | exact | **1.00** |
| `kind=etch, mechanism=ellipsoidal` | `DirectionalProcess([0,0,1], {mat:(vertical, lateral)})` | `etch_ellipsoidal(filter, vertical, lateral)` | approximate（模型不同，本例 1.00） | **1.00** |
| `kind=mask, mechanism=litho` | 同一份 GDS 层 → 逐**轴对齐矩形** `makeBoxStencil` 切/加 + 0.1 µm 涂层 | `litho_patterning(LayerNames=[name])` | approximate（三条件成立时 1.00） | **1.00** |

`engines/examples/m3-four-ops.json` 是这条验收用例（Memory1 窗口 0.624×0.74 µm = 0.46 µm²，
在 1.0 µm² 安全限内）：**5 步全部对齐，逐步一致率 100%，截面偏差 4e-6 µm**，
ViennaPS 6.1 s / SPX 28 s。

对拍过程中定死了三件事（这才是 100% 的原因）：

1. **层名↔层号映射**（六个名字全部 144/144 逐点吻合）：
   `M1=9, PLG=11, CS=15, BC=17, PC=19, M2=21`。方法：先各向异性刻蚀掉未被胶保护的
   SiO₂ 50 nm，再在 z=0.09 µm 打 12×12 probe 网格，得到"每个 name 的开口图"，
   与该层多边形覆盖率的取反逐点比对；极性同时确认为 **negative（有图形处去胶）**。
   注意 BC/17 证据较弱：它的 16 个接触孔小于探测栅距，两侧在网格上都没看到覆盖。
2. **涂层厚度 0.1 µm**：SPX 的 `litho_patterning` 是完整光刻（coat+expose+develop），
   会在现有胶上再加一层（实测胶顶 0.15 → 0.25）。ViennaPS 用 `parameters.coat`
   （默认 0.1）复现，两边因此是**同一个实体**，不只是同一个轮廓。
3. **材料名**：工作台 `PMMA` == SPX `Photoresist`；写成 `Mask` 会让同一块胶标签不同，
   白掉 20% 的一致率。

三个实测限制（都写进代码注释）：

1. **`fill` 的构造**：`ps.Box` 覆盖整个窗口 → 与每个已有实体做 `RELATIVE_COMPLEMENT`
   → 作为新材料插入。实测 0.08 µm 沟槽会被填到底并留盖层，与 SPX `depo_fill` 的形状一致。
   注意 `MakePlane(addToExisting=True)` **不能**用来做 fill（实测只在顶面加一层，沟槽原样保留）。
2. **`litho` 只能放轴对齐矩形**：`GDSGeometry.layerToLevelSet()` 返回的 LS 与仿真域
   做布尔会 **segfault**（实测两例），所以走工作台同款 `GeometryFactory.makeBoxStencil`。
   Memory1 的 15/17/21 层正好是矩形（30/16/16 个），9/11/19 层是多边形（5/6/8、52/100 顶点），
   目前会计入 "skipped polygon" 而不近似。
3. **层号↔层名映射：已定**（144/144，见上方表格）。

**`etch_ellipsoidal` 的参数形状**（踩过）：每个材料项是
`(filter, vertical_rate, lateral_rate)` **三个位置参数**，不是嵌套的两元组；
写错时 SPX 报 `Cannot convert '(1.0, qup.Unit('um min^-1'))' to double`。

## 5.10 Memory1 分步对拍（进行中）

Memory1 的 88 步被拆成模块逐个搬进来，每步都跑双引擎逐点对拍。

**已完成：前 7 步（Wafer Start → ON_Stack_Top → Plug_Patterning 的沉积）**

`engines/examples/memory1-prefix.json`，7 步全部对齐，**逐步一致率 100%、截面偏差 2e-6 µm**：

```
substrate(Si 0.3) → depo_fill Oxide2 50nm → depo_fill Nitride 35nm
→ depo_fill Oxide 28nm → depo_fill Oxide 50nm → depo_fill SiOC 100nm
→ depo_fill BARC 50nm
```

为此新增 `engines/materials.py`：把 Memory1 用到但 4.7.0 没有的材料
（`Oxide2 SiOC BARC CESL；SiON/TaN 本来就有`）注册成工作台材料（自定义 id 从 177 起），
避免把多个 SPX 材料并到同一个工作台材料上导致层被合并。

**已跑通但尚未对齐：Plug_Patterning 的掩膜与刻蚀**

`engines/examples/memory1-plug.json`（同样的 7 步 + `litho PLG(层 11)` + `etch BARC/SiOC` + strip）：

| 步 | 一致率 | 说明 |
|---|---|---|
| s7 `litho PLG` | 0.87 | 非矩形多边形已能生成（棱柱 → FromSurfaceMesh），差异是**胶体本身**的涂层轮廓 |
| s8 `etch BARC/SiOC` | 0.83 | 改为几何切法后从 0.20 升上来；仍差在 PMMA |
| **s9 strip（模块结果）** | **1.00** | 去掉胶之后两边**完全一致** |
| s10 `etch ON`（5 µm 过刻） | 0.83 | 掩膜把刻蚀挡在 ~0.4 µm，差在 SiOC/PMMA |
| **s11 strip SiOC（模块结果）** | **1.00** | 一致 |

规律很清楚：**每个模块"去掉胶之后"的状态都是 1.00**，差异集中在胶体本身
（SPX 的 litho 涂层轮廓与我们的长方体涂层不同）。也就是说几何结果对得上，
中间态的胶形是已知差异。

顺带修了域守卫：`etch ON` 是 Memory1 的 5 µm 过刻步，原先被 `ratio` 守卫判成
`out_of_domain`。掩膜刻蚀的位移不可能超过暴露的叠层，所以
`limits.estimate` 对带 `mask` 的刻蚀步把位移夹到 `domain.height`。

**硬限制**：ViennaPS 的多级水平集是"**层栈**"——材料 i 占据 LS_i 与 LS_{i−1} 之间的区域。
刻蚀一个**被覆盖的**材料时，上面的材料会跟着界面一起下沉，而不是留下空腔；
所以"图案化后刻蚀下层"这一步在层栈里没有真解。SPX 是 CSG/自由体模型，天然可以。

现在已经有的两条绕过方案：

1. `_etched_openings`（几何切法）：知道开口多边形时，直接在开口内切掉指定深度，
   避开原生 masked `DirectionalProcess`（它在"顶层 LS 带洞"的域上会 segfault，已复现两例）。
   在 `engines/examples/mask-etch.json` 上验证：litho 0.98 / 刻蚀 0.96 / strip 1.00。
2. `PMMA` 命名与 0.1 µm 涂层（见 §5.9）——否则同一块胶在两边标签/厚度都不同。

下一步（未做）：把所有"图案化后刻蚀"的步骤都改走 `_etched_openings` 的几何切法，
并支持多材料/刻到掩膜底 的语义，然后把 Plug_Patterning 剩余步与后续模块逐个接上。

## 6. 怎么跑

```bash
# 单独跑一个引擎
.venv/bin/python engines/viennaps/engine.py engines/examples/m0-plane-isoetch.json var/m0/viennaps
$SPX/linux64/bin/python3 engines/spx/bridge.py engines/examples/m0-plane-isoetch.json var/m0/spx

# 两个一起，出 compare.json
.venv/bin/python -m engines.cli run engines/examples/m0-plane-isoetch.json \
  --engines viennaps,spx --out var/m0/compare

# 服务里（三页前端在 #compare）
WORKBENCH_PORT=8773 VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/e2e .venv/bin/python -m backend.serve
# 浏览器打开 http://127.0.0.1:8773/#compare
```

SPX 适配器需要 license 与 `SNPSLMD_LICENSE_FILE`（默认 `27000@localhost`），
并自动带 `LD_PRELOAD=/opt/eda-patches/libfastrefuse.so`（WSL2 mirrored 下无监听端口的
connect 会永久挂住）。

## 7. 验收（已测）

| 项 | 结果 |
|---|---|
| 平面 + SiO2/Si 各向同性刻蚀，两引擎都出帧 | ✅ |
| `compare.json` 逐步对齐（2/2） | ✅ |
| 每步耗时：ViennaPS ~0.6–2 s / SPX ~0.3 s（含 0.5 s 取帧） | ✅ |
| 全流程 wall：ViennaPS ~1.3 s / SPX ~5.7 s（SPX 含 1.4 s 进程启动 + layout 载入） | ✅ |
| 三页前端可用（几何 / 流程图 / 左右对比 + 时间轴 + 表） | ✅ |
| **M1 公共探针格 3000 点：两步一致率 100%、Si/SiO2 IoU 1.0** | ✅ |
| **M1.5 截面 33 列：Si 4e-6 µm、SiO₂ 8.3e-5 µm 平均界面偏差** | ✅ |
| **M2 各向异性（a=0.8）：逐点一致率 100%，截面 6e-6 µm** | ✅ |
| **M3 mesh 视图换成 vtk.js（复用 WorkflowViewer3D，共享相机方位/仰角）** | ✅ |
| M2 域守卫：2500Δx 位移 → 两引擎 `out_of_domain`，后续步骤不再推进 | ✅ |
| M2 checkpoint/restore：从 `s1_aniso.vpsd` 恢复后顶高 0.05 µm，再 CMP 到 0.02 µm | ✅ |
| M1.5 视角预设：SPX `View=iso/xz/...`，左侧同方位投影 | ✅ |
| M1 每材料顶高两边完全一致（刻蚀前 SiO2 0.0809 µm，刻蚀后 0.0397 µm，格点分辨率内） | ✅ |
| `tests/test_engines.py`（协议/调度/对齐/幂等 11 项） | ✅ |
| `tests/test_engine_probe.py`（格点/占用/截面/相机/守卫/归一化 28 项） | ✅ |
| `tests/test_compare_api.py`（4 项，含 422/404/路径穿越） | ✅ |
| 前端 `npm run typecheck` / build | ✅ |

## 8. 已知差异（必须写在界面上，不能藏）

1. **ViennaPS 侧只有水平界面**：周期边界 + 平面结构的表面网格没有侧壁，
   而 SPX 渲染的是被 clip 出来的柱体（带人工侧壁）。这是表示差异，不是数值差异。
   等 phase B 接上掩膜/沟槽，两侧都有真实侧壁，对比才有意义。
2. **相机**：SPX 截图目前不能传相机，所以右侧固定；左侧 mesh 用共享的
   方位/仰角/缩放（同一 bbox）。M1.5 起：预设（`View=iso|xz|…`）或**自由相机**
  （`View=free` + focal/position），左侧 `cameraForView` 按同一方位投影；
  仰角 >85° 的正上方退回预设（SPX 的 up 向量退化）。
3. **SPX 只用 1 个 licence seat**（`spx_all`），并发对比运行会排队。

## 9. 下一步

- **M1 / M1.5 / M2 已完成**：公共探针格、截面接口高度、视角预设（含自由相机）、
  各向异性、CMP 判定、成本守卫、checkpoint/restore。
- **仍缺**：3D 界面单向 Hausdorff / 轮廓 L2 需要结构导出；`native`/`tdr` 是 HDF5，
  要么给引擎层引入 HDF5 读取依赖，要么等 SPX 提供可解析的非 HDF5 导出。在那之前
  只报截面（z 方向）偏差，不编 3D 距离。
- **M2**：各向异性 / CMP / 掩膜接线；`checkpoint()/restore()` 热换引擎；
  成本模型 `estimate()` 与越界守卫（`out_of_domain`，不静默回退）。
- **M3 完成**：mesh 视图接回 vtk.js（复用 `WorkflowViewer3D`，`SnapshotDoc` 适配层在
  `frontend/src/compare/MeshPane.tsx`）。
- **仍未完成**：掩膜 / GDS mask（§5.6）；SPX `etch_cmp` 语义（CMP 仍 `no_counterpart`）。
