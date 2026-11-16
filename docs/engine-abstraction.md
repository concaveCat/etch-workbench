# 引擎抽象与双引擎对比（Phase A：M0 / M1 / M1.5）

> 状态：M0 + M1 + M1.5 已落地——协议、调度器、两个适配器、公共探针格、截面指标、
> 对比 API、三页前端、测试。本文只描述已经能跑的东西，以及明确**还没做**的东西（M2/M3）。

## 1. 目标与边界

同一份编译好的流程，在两个引擎上各跑一遍，逐步对齐、逐步计时、左右对照。
本轮只做**两端都支持的几何子集**：

| step | ViennaPS 侧 | SPX 侧 | 可比性 |
|---|---|---|---|
| 初始结构（平面 + 薄膜栈） | `MakePlane` | `substrate` + `depo_isotropic` | exact |
| 各向同性刻蚀/沉积 | `IsotropicProcess` + `setMaterialRate` | `etch_isotropic` / `depo_isotropic` | exact |
| 各向异性刻蚀/沉积 | `DirectionalProcess`（未接） | `etch_anisotropic` / `depo_anisotropic` | 已声明，未接线 |
| 平坦化 | geometric planarize（未接） | `etch_cmp` | approximate，未接线 |

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

**相机**：`meas_screenshot(View=…)` 实测只接受
`{ free, iso, iso90, iso180, iso270, xy, xz, yx, yz, zx, zy }`，其它值直接报错；
除 `free` 外的显式相机参数（azimuth/elevation/zoom）在无 GUI 环境没有出口
（`control_screenshot*` 只暴露 `Show Axes`，`sp.show(Children=['Snapshot'])` 为空）。
所以做法是**流程里选一个 View 预设**，左侧网格按同一方位投影（`cameraForView`），
左右共用预设而不是任意相机。

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
| M1.5 视角预设：SPX `View=iso/xz/...`，左侧同方位投影 | ✅ |
| M1 每材料顶高两边完全一致（刻蚀前 SiO2 0.0809 µm，刻蚀后 0.0397 µm，格点分辨率内） | ✅ |
| `tests/test_engines.py`（协议/调度/对齐/幂等 11 项） | ✅ |
| `tests/test_engine_probe.py`（格点/占用指标/截面指标/材料归一化 18 项） | ✅ |
| `tests/test_compare_api.py`（4 项，含 422/404/路径穿越） | ✅ |
| 前端 `npm run typecheck` / build | ✅ |

## 8. 已知差异（必须写在界面上，不能藏）

1. **ViennaPS 侧只有水平界面**：周期边界 + 平面结构的表面网格没有侧壁，
   而 SPX 渲染的是被 clip 出来的柱体（带人工侧壁）。这是表示差异，不是数值差异。
   等 phase B 接上掩膜/沟槽，两侧都有真实侧壁，对比才有意义。
2. **相机**：SPX 截图目前不能传相机，所以右侧固定；左侧 mesh 用共享的
   方位/仰角/缩放（同一 bbox）。M1.5 起改为**共用视角预设**：右侧 `View=iso|xz|...`，
  左侧 `cameraForView` 按同一方位投影；任意相机参数在无 GUI 环境下没有出口。
3. **SPX 只用 1 个 licence seat**（`spx_all`），并发对比运行会排队。

## 9. 下一步

- **M1 / M1.5 已完成**：公共探针格（一致率 / IoU / 顶高）、截面接口高度
  （mean/max/L2）、视角预设。
- **仍缺**：3D 界面单向 Hausdorff / 轮廓 L2 需要结构导出；`native`/`tdr` 是 HDF5，
  要么给引擎层引入 HDF5 读取依赖，要么等 SPX 提供可解析的非 HDF5 导出。在那之前
  只报截面（z 方向）偏差，不编 3D 距离。
- **M2**：各向异性 / CMP / 掩膜接线；`checkpoint()/restore()` 热换引擎；
  成本模型 `estimate()` 与越界守卫（`out_of_domain`，不静默回退）。
- **M3**：mesh 侧换回 vtk.js（现在用 canvas 正交投影），raster 侧接入同一相机参数。
