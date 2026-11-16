# 引擎抽象与双引擎对比（Phase A / M0）

> 状态：M0 已落地——协议、调度器、两个适配器、对比 API、三页前端、测试。
> 本文只描述已经能跑的东西，以及明确**还没做**的东西（M1/M2/M3）。

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

## 5. 指标：说清楚什么是没测的

`engines/metrics.py`：

- mesh 帧 → 每材料**顶高**（相对 z=0）与三角形数（M0 实测 SiO2 顶高 0.05 µm，
  即 0.1 µm 沉积层被 1.0 µm/min × 0.05 min 刻掉一半）；
- raster 帧 → 只有帧大小，几何记 `not_evaluated`，理由写进 JSON。

**没有**跨引擎逐点距离。因为现在 SPX 只回渲染帧、不回结构，任何 mesh-vs-raster
距离都是编的。M1 要做的是：从 SPX 导出结构（`control_save_3D` / TDR）→
公共探针格（同一 Δx、材料占用标签）→ 每材料 IoU / 界面单向 Hausdorff / 轮廓 L2。

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

## 7. M0 验收（已测）

| 项 | 结果 |
|---|---|
| 平面 + SiO2/Si 各向同性刻蚀，两引擎都出帧 | ✅ |
| `compare.json` 逐步对齐（2/2） | ✅ |
| 每步耗时：ViennaPS ~0.6–2 s / SPX ~0.3 s（含 0.5 s 取帧） | ✅ |
| 全流程 wall：ViennaPS ~1.3 s / SPX ~5.7 s（SPX 含 1.4 s 进程启动 + layout 载入） | ✅ |
| 三页前端可用（几何 / 流程图 / 左右对比 + 时间轴 + 表） | ✅ |
| `tests/test_engines.py`（协议/调度/对齐/幂等 11 项） | ✅ |
| `tests/test_compare_api.py`（4 项，含 422/404/路径穿越） | ✅ |
| 前端 `npm run typecheck` / build | ✅ |

## 8. 已知差异（必须写在界面上，不能藏）

1. **ViennaPS 侧只有水平界面**：周期边界 + 平面结构的表面网格没有侧壁，
   而 SPX 渲染的是被 clip 出来的柱体（带人工侧壁）。这是表示差异，不是数值差异。
   等 phase B 接上掩膜/沟槽，两侧都有真实侧壁，对比才有意义。
2. **相机**：SPX 截图目前不能传相机，所以右侧固定；左侧 mesh 用共享的
   方位/仰角/缩放（同一 bbox）。真正同机位需要 SPX 侧相机参数支持，列在 M1。
3. **SPX 只用 1 个 licence seat**（`spx_all`），并发对比运行会排队。

## 9. 下一步

- **M1**：SPX 结构导出 + 公共探针格 + 跨引擎指标（IoU、界面距离、顶高误差）；
  SPX 相机参数化。
- **M2**：各向异性 / CMP / 掩膜接线；`checkpoint()/restore()` 热换引擎；
  成本模型 `estimate()` 与越界守卫（`out_of_domain`，不静默回退）。
- **M3**：mesh 侧换回 vtk.js（现在用 canvas 正交投影），raster 侧接入同一相机参数。
