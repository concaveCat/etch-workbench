# 引擎具体算法记录与配置注册表（Engine Algorithm Registry）

本文档与可执行配置表 [`engines/algorithms.json`](../engines/algorithms.json)、[`engines/algorithms.py`](../engines/algorithms.py) 同步维护，记录平台内全部引擎（`voxel`、`viennaps`、`spx`）在每一道工艺工序与底层几何算子上**具体采用的算法类型、理论/契约出处、近似边界**，并说明如何**一行命令查询、切换或注册新算法**。

---

## 1. 快速查询与修改命令

无需翻找源码，随时可在终端查看实时算法表或切换采用的算法：

```bash
cd /root/wb-engines

# 1) 查看全平台所有引擎的工序算法表（带 * 号表示当前默认生效算法）
.venv/bin/python -m engines.cli algorithms

# 2) 只看单个引擎或导出机器可读 JSON（也可请求 GET /api/compare/algorithms）
.venv/bin/python -m engines.cli algorithms --engine voxel
.venv/bin/python -m engines.cli algorithms --json

# 3) 永久切换某道工序的全局默认算法（自动校验算法是否已注册，原子写回 engines/algorithms.json）
.venv/bin/python -m engines.cli set-algorithm voxel mask/litho voxel.litho.gaussian_aerial
.venv/bin/python -m engines.cli set-algorithm voxel etch/ellipsoidal voxel.etch.linear_depth_taper

# 4) 不改全局配置，仅在单次对拍运行中临时切换算法（支持重复传多个 --algo）
.venv/bin/python -m engines.cli run engines/examples/memory1-plug.json \
  --engines voxel,spx --reference spx --sequential \
  --algo mask/litho=voxel.litho.gaussian_aerial \
  --out var/exp_gaussian_litho
```

### 算法路由的四级优先级（从高到低）

1. **单步显式指定（`StepIR.model`）**：JSON 中某一步写 `"model": "voxel.cmp.dishing"`，仅覆盖该步；
2. **单次运行指定（`JobSpec.algorithms` / CLI `--algo`）**：例如 `--algo mask/litho=voxel.litho.gaussian_aerial`（或限定引擎 `--algo voxel:mask/litho=...`），覆盖本次运行中所有匹配的工序；
3. **全局默认配置表（`engines/algorithms.json` 中的 `active_defaults`）**：直接编辑该 JSON 或用 `set-algorithm` 修改；
4. **代码内置回退（`@registry.register(kind, mechanism)`）**。

> 每一步实际执行所采用的算法 ID 与算法类型都会自动写入 `result.json` 与 `compare.json` 的 `metrics.model`、`metrics.algorithmType`、`metrics.algorithmFidelity`，确保任何一次实验结果均可溯源。

---

## 2. 算法类型分类标准（`algorithm_type`）

| 类型标识 | 含义 | 精度性质 |
|---|---|---|
| `exact_morphology` | **精确三维数学形态学包络**：基于精确欧氏距离变换（EDT）的球膨胀/腐蚀，单材料常速率下与连续 Hamilton–Jacobi 方程粘性解严格等价 | `exact`（网格步长内无模型近似） |
| `ray_column` | **逐列视线积分模型**：沿 $-Z$ 方向逐 `(x, y)` 柱积分消耗刻蚀时间预算，遇停止层即停并保留悬空阴影 | `exact`（理想完全各向异性下精确） |
| `contract_macro` | **工艺语义宏算子**：对齐 SPX 公开接口定义的整步几何动作（凹槽盖层填充、停止面截断 CMP） | `exact`（与契约定义一致） |
| `optical_aerial` | **高斯光学空中像圆角模型**：二维高斯卷积模拟光学衍射拐角圆化 + 阈值显影 | `empirical`（可调光学参数 `sigma`） |
| `geometric_envelope` | **深度插值锥面几何包络**：侧向扩孔半径随刻蚀深度线性递减的连续锥角侧壁 | `approximate` |
| `first_order_approx` | **一阶简化几何近似**：算子分裂或平均半宽直筒等快速基准实现 | `approximate` |
| `empirical_physical` | **经验物理效应模型**：针对非理想工艺效应（如 CMP 金属碟形凹陷、RIE Lag）的参数化模型 | `empirical` |
| `pde_levelset` | **水平集 PDE 时间步进**：ViennaPS 连续表面速度场 Hamilton–Jacobi 窄带推进 | `approximate`（受层栈拓扑限制） |
| `csg_boolean` | **显式几何体布尔运算**：棱柱/板层网格或水平集的并集、补集裁剪 | `exact` / `approximate` |
| `reference_blackbox` | **商业参考引擎原生内核**：Synopsys SPX 闭源 `geocolumns` 原生算子（对标真值） | `exact`（参考基准） |

---

## 3. 底层公共几何与采样算子记录表

| 引擎 | 算子 ID | 算法类型 | 算法来源 / 文献出处 | 源码位置 | 说明 |
|---|---|---|---|---|---|
| `voxel` | `voxel.edt.fh_slabbed_3d` | `exact_morphology` | Felzenszwalb & Huttenlocher (2012), *Theory of Computing* 8:415–428 | `engines/voxel/_native.cpp:run_slabbed_3d_dilation` | Z 方向 64 层流式分块 + 周期边界抛物线下包络精确 3D 欧氏距离平方变换；时间严格 $O(N)$（与沉积/刻蚀厚度无关），12.84 亿体素下临时内存仅 `~118 MB` |
| `voxel` | `voxel.topology.seeded_cavity_flood` | `exact_morphology` | 三维 6-邻域数字拓扑连通搜索 + 埋藏空洞列索引剪枝 | `engines/voxel/_native.cpp:compute_exposed_void_mask_internal` | 顶面气源连通域判别；无悬空步一次视线快扫完成，含替换栅腔体步仅对有埋藏空洞的列做正反向扫描，保护内部封死缝隙 |
| `voxel` | `voxel.raster.scanline_polygon` | `csg_boolean` | Jordan 曲线奇偶射线交叉法（OpenMP 并行） | `engines/voxel/_native.cpp:voxel_rasterize_polygons` | 将 GDS/OASIS 多边形光栅化为二维体素中心掩膜 |
| `viennaps` | `viennaps.probe.ray_winding` | `csg_boolean` | 界面三角网格竖直射线奇偶绕数法 | `engines/viennaps/engine.py:_ensure_ray_bins` | 在 `getSurfaceMesh(addInterfaces=True)` 上精确解析重复材料多层栈与空腔 |
| `spx` | `spx.probe.meas_thickness_geocolumns` | `reference_blackbox` | SPX `sp.measure(Type="meas_thickness")` | `engines/spx/bridge.py:intervals_from_thickness_output` | 一次查询提取 `(x, y)` 柱从底到顶完整连续浮点区间（含埋藏 `Gas` 腔体） |

---

## 4. 各工序具体算法对照与可选算法总表

### 4.1 `voxel` 引擎（主力高速体素引擎，全部可配置/热插拔）

| 工序槽位 `slot` | 默认生效 | 算法 `model_id`（及短别名） | 算法类型 | 算法来源与数学规则 | 已知近似边界 / 可调参数 | 源码位置 |
|---|---|---|---|---|---|---|
| `deposit/fill` | **`*`** | `voxel.fill.gas_overburden` | `contract_macro` | 对齐 SPX `depo_fill`：将 `z < surface_top + offset` 内所有连通暴露气相置为目标材料 | 精确保持封闭气隙不填充 | `engines/voxel/models/deposit.py` |
| `deposit/isotropic` | **`*`** | `voxel.depo.fh_euclidean_ball` | `exact_morphology` | Serra 3D 欧氏球膨胀 + FH 精确线性 EDT（半径 $r = \text{rate}\cdot t$） | 单材料常速率下为连续 PDE 精确解 | `engines/voxel/models/deposit.py` |
| `deposit/anisotropic` | **`*`** | `voxel.depo.split_dir_iso` | `first_order_approx` | Lie–Trotter 算子分裂：`anisotropy` 比例纯 $+Z$ 柱状生长 + `(1-anisotropy)` 球膨胀 | $0 < a < 1$ 时拐角为圆弧而非斜折面 | `engines/voxel/models/deposit.py` |
| `etch/anisotropic` | **`*`** | `voxel.etch.line_of_sight_column` | `ray_column` | 1D 视线柱积分：自顶向下按 $\Delta t = \sum \Delta z_i / r_i$ 逐层刻蚀，遇 $r=0$ 掩膜/停止层即停 | $a=1$ 时精确；$a<1$ 叠加各向同性分量 | `engines/voxel/models/etch.py` |
| `etch/isotropic` | **`*`** | `voxel.etch.substepped_fh_erosion` | `exact_morphology` | 从连通暴露气相做 FH 3D 欧氏球腐蚀（按 $\le 15\text{ nm}$ 位移自动切分子步） | 多材料交界折转精度由 `15 nm` 子步长控制 | `engines/voxel/models/etch.py` |
| `etch/ellipsoidal` | **`*`** | `voxel.etch.cylindrical_halfwidth` | `first_order_approx` | 垂向视线直刻 + 暴露侧壁统一水平圆盘扩孔 $0.5 \cdot r_{\text{lat}} \cdot t$ | 将斜锥侧壁近似为平均半宽直筒 | `engines/voxel/models/etch.py` |
| `etch/ellipsoidal` | 可选 | `voxel.etch.linear_depth_taper`（别名 `ellipsoidal_taper`） | `geometric_envelope` | 垂向直刻后在刻蚀高度带内分 12 层线性插值扩孔（槽口扩 $r_{\text{lat}} t$，槽底扩 $0$） | 阶梯分带逼近连续椭球锥角侧壁 | `engines/voxel/models/etch.py` |
| `mask/litho` | **`*`** | `voxel.litho.hard_polygon` | `first_order_approx` | 硬边界多边形光栅化 + 对齐 SPX `coat = 0.1 µm` 负胶涂覆开孔 | 掩膜拐角为理想 90° 尖角，无光学扩散圆角 | `engines/voxel/models/litho.py` |
| `mask/litho` | 可选 | `voxel.litho.gaussian_aerial`（别名 `litho_gaussian`） | `optical_aerial` | 可分离二维周期高斯模糊 + `threshold` 阈值显影开孔 | 支持 `parameters.sigma`（默认 `0.010 µm`）与 `threshold`（默认 `0.5`） | `engines/voxel/models/litho.py` |
| `strip/strip` | **`*`** | `voxel.strip.exact_id_clear` | `exact_morphology` | 指定材料整数 ID 全域精确清零为 `void` | 无近似 | `engines/voxel/models/litho.py` |
| `planarize/cmp` | **`*`** | `voxel.cmp.ideal_stop_plane` | `contract_macro` | 对齐 SPX `etch_cmp`：在 `max_z(stop_material) + offset` 水平面截断 | 理想刚性抛光垫，无图形密度凹陷 | `engines/voxel/models/planar.py` |
| `planarize/cmp` | 可选 | `voxel.cmp.dishing`（别名 `cmp_dishing`） | `empirical_physical` | 在停止面截断后，对暴露软金属区（如 `Cu`）下挖 `max_dishing_um` | 支持 `metal_material`（默认 `"Cu"`）、`max_dishing_um`（默认 `0.008 µm`） | `engines/voxel/models/planar.py` |

---

### 4.2 `viennaps` 与 `spx` 算法对照简表

| 工序槽位 `slot` | `viennaps` 算法 ID（类型） | `spx` 参考算法 ID（类型） |
|---|---|---|
| `deposit/fill` | `viennaps.fill.box_complement`（`csg_boolean`） | `spx.procedural.depo_fill`（`reference_blackbox`） |
| `deposit/isotropic` | `viennaps.depo.ls_isotropic_pde`（`pde_levelset`） | `spx.procedural.depo_isotropic`（`reference_blackbox`） |
| `deposit/anisotropic` | `viennaps.depo.ls_directional_pde`（`pde_levelset`） | `spx.procedural.depo_anisotropic`（`reference_blackbox`） |
| `etch/anisotropic` | `viennaps.etch.stencil_or_slab_cut`（`csg_boolean`） | `spx.procedural.etch_anisotropic`（`reference_blackbox`） |
| `etch/isotropic` | `viennaps.etch.ls_isotropic_pde`（`pde_levelset`） | `spx.procedural.etch_isotropic`（`reference_blackbox`） |
| `etch/ellipsoidal` | `viennaps.etch.ls_directional_taper`（`pde_levelset`） | `spx.procedural.etch_ellipsoidal`（`reference_blackbox`） |
| `mask/litho` | `viennaps.litho.prism_mesh_boolean`（`csg_boolean`） | `spx.procedural.litho_patterning`（`reference_blackbox`） |
| `strip/strip` | `viennaps.strip.remove_material_ls`（`csg_boolean`） | `spx.procedural.etch_strip`（`reference_blackbox`） |
| `planarize/cmp` | `viennaps.cmp.ls_planarize_plane`（`csg_boolean`） | `spx.procedural.etch_cmp`（`reference_blackbox`） |

---

## 5. 如何注册你自己的新算法并纳入记录表

### 方式 A：在外部实验插件中注册（零改动仓库源码，自动出现在 `algorithms` 表）

新建 `my_arde_plugin.py`，使用 `@registry.register_model` 填写完整元数据：

```python
from engines.voxel.models.etch import _top_down_column_etch


def register_models(registry):
    @registry.register_model(
        "voxel.etch.arde_coburn_winters",
        slot="etch/anisotropic",
        algorithm_type="empirical_physical",
        fidelity="empirical",
        origin="Coburn & Winters Conductance-Limited RIE Lag Flux Model",
        summary="按局部开孔深宽比衰减底部有效刻蚀速率，模拟高深宽比接触孔 RIE Lag。",
        aliases=("arde_etch",),
    )
    def etch_with_arde(state, step, ctx):
        rates = {
            state.material_id(mat): abs(float(r))
            for mat, r in step.rates.items()
            if float(r) < 0.0
        }
        removed = _top_down_column_etch(state, rates, float(step.duration))
        return {"removedVoxels": removed}
```

挂载插件即可查询并直接切换使用：

```bash
# 1) 验证新算法已自动出现在 voxel 的 etch/anisotropic 槽位下
VIENNAPS_STEP_MODEL_MODULES=$PWD/my_arde_plugin.py \
  .venv/bin/python -m engines.cli algorithms --engine voxel

# 2) 用 --algo 将全流程所有 etch/anisotropic 步切换到你的新算法，与 SPX 同台对拍
VIENNAPS_STEP_MODEL_MODULES=$PWD/my_arde_plugin.py \
  .venv/bin/python -m engines.cli run engines/examples/memory1-plug.json \
  --engines voxel,spx --reference spx --sequential \
  --algo etch/anisotropic=voxel.etch.arde_coburn_winters \
  --out var/exp_arde
```

### 方式 B：沉淀为内置长期算法

1. 将上述 `@registry.register_model(...)` 函数放入 `engines/voxel/models/` 下的对应文件（`deposit.py` / `etch.py` / `litho.py` / `planar.py`）；
2. 若希望将其设为全局默认算法，运行：
   ```bash
   .venv/bin/python -m engines.cli set-algorithm voxel etch/anisotropic voxel.etch.arde_coburn_winters
   ```
3. 在本文档 §4.1 表格中补充一行记录，保持代码与文档一致。
