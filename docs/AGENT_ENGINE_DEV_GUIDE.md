# Agent 仿真测试与下一代引擎开发指南

本文档面向在本项目（`/root/wb-engines`）中执行任务的自动化 Agent，提供**零猜测、可直接复制执行**的操作手册，涵盖两件事：

1. **如何构造并输入自定义测试样例（StepIR + 版图），与参考引擎（Synopsys SPX）同台仿真对拍**；
2. **如何在此多引擎基座上进行下一步工艺模型研发、算子级 GPU/C++ 加速或接入全新求解器引擎**。

相关设计背景参见 [multi-engine-architecture.md](./multi-engine-architecture.md) 与 [engine-abstraction.md](./engine-abstraction.md)。

---

## 1. 环境、目录与安全红线（动手前必读）

### 1.1 仓库与工作区隔离

| 项目 | 路径 / 规则 |
|---|---|
| 主仓库（**严禁直接改动**） | `/root/viennaps-workbench-cpu-gpu`（`main` 分支） |
| 引擎开发工作区 | `/root/wb-engines`（`task/engines` 分支）；若多 Agent 并行开发不同特性，基于 `task/engines` 另开 `git worktree` |
| Python 虚拟环境 | `/root/wb-engines/.venv/bin/python`（含 `viennaps`、`numpy`、`gdstk`、`fastapi`） |
| SPX 私有运行时 | `/root/SPX/spx/V-2024.03/linux64/bin/python3`（由 `engines/spx/bridge.py` 子进程自动调用，**不要**混入 `.venv`） |
| WebUI 对比预览地址 | `http://127.0.0.1:8801/#compare`（`var/listen-port.json` 固定在 `8801` 端口） |

### 1.2 绝对禁忌（真实踩坑记录）

1. **严禁触碰宿主机网络与 License 环境**：
   - 不要修改任何网络接口、路由、DNS、MAC、hostname、`/etc/hosts` 或 `loopback0`（详见 `/root/AGENTS.md`）。
   - 怀疑 EDA / License 异常时，先只读运行 `/opt/eda-patches/verify.sh`（退出码 `0` 表示正常）。
2. **严禁绕过 SPX 窗口面积护栏（OOM 会直接拖垮重启整台 WSL2）**：
   - SPX 会把整个 `clip` 窗口一次性建入内存，自身无上限保护。**窗口面积必须 `<= 1.0 µm²`**（由 `engines/limits.py: window_verdict` 与 `RLIMIT_AS=6144 MiB` 双重拦截）。
   - 任何涉及 SPX 的测试**必须**走 `python -m engines.cli` 或 `engines/spx/bridge.py`，**禁止**手写裸脚本对大窗口直接调 `spx.procedural`。
3. **SPX 只有 1 个 `spx_all` License Seat**：
   - 跑包含 `spx` 的对比或测速时，统一加 `--sequential`，且同一时刻不要并行起多个 SPX 任务。
4. **WSL2 本地端口连接必带超时或 `libfastrefuse.so`**：
   - WSL2 mirrored 网络对未监听的本地端口会丢弃 SYN 导致阻塞 `connect()` 永久挂死。`engines/registry.py` 已自动为 SPX 注入 `LD_PRELOAD=/opt/eda-patches/libfastrefuse.so`；若自己在 Python 中探测端口，必须设 `sock.settimeout(0.2)`。
5. **沙箱（Sandbox）说明**：
   - 常规文件读写、单元测试、`voxel` / `viennaps` 仿真、前端编译均可在无网络沙箱内直接运行。
   - **唯独运行 `spx` 引擎**需要连接本机 `27000@localhost` FlexNet License 服务；若沙箱禁用了 loopback 网络（报 `FlexNet Licensing error: -15,234`），仅对含 `spx` 的那条 CLI 命令申请沙箱外执行即可。

---

## 2. 如何自己构造并输入测试样例进行仿真

所有引擎（`voxel`、`viennaps`、`spx` 以及你新写的引擎）共享唯一输入契约：`JobSpec` JSON（定义在 `engines/protocol.py`）。

### 2.1 单位与坐标系铁律

在编写任何测试 JSON 前，必须遵守以下写死在协议层的物理约定：

- **长度单位**：微米（`um`）。例如 `50 nm` 写作 `0.05`，`5 nm` 写作 `0.005`。
- **时间单位**：分钟（`min`）；**速率单位**：`um/min`。
- **速率正负号**：**刻蚀（去除）速率必须为负数**（如 `"-1.0"`），**沉积速率必须为正数**（如 `"1.0"`）。未出现在 `rates` 字典中的材料默认选择比为 `0`（完全不刻蚀）。
- **Z 轴约定**：`z` 轴垂直向上，**衬底（substrate）顶面恒为 `z = 0.0`**。衬底占据 `[-substrate_thickness, 0.0]`，薄膜与后续工艺向 `+z` 生长，计算域总范围为 `[-substrate_thickness, domain.height]`。
- **XY 局部坐标与 `origin_xy`**：`StepIR`、探针（`probe`）和截面（`section`）始终使用局部窗口坐标 `[0..width, 0..depth]`。若版图裁剪自全局坐标 `[x0, y0, x0+width, y0+depth]`，将 `[x0, y0]` 填入 `domain.origin_xy`，引擎会自动平移对齐。
- **标准材料名（Canonical Material Names）**：
  务必使用 `engines/normalize.json` 中的标准工作台材料名，框架会自动翻译为 SPX 材料名（例如 `Si` ↔ `Silicon`、`SiO2` ↔ `Oxide`、`Si3N4` ↔ `Nitride`、`PolySi` ↔ `PolySilicon`、`W` ↔ `Tungsten`、`Cu` ↔ `Copper`、`Al2O3` ↔ `AlO`、`PMMA` ↔ `Photoresist`）。
  > **注意**：光刻胶请统一写 `"PMMA"`（映射 SPX `Photoresist`）；写成 `"Mask"` 虽然能跑，但审计或部分掩膜判断优先识别 `"PMMA"`。

### 2.2 全部工艺步骤（`StepIR`）写法速查表

每个步骤包含固定骨架字段：
`step_id`（唯一字符串）、`kind`、`mechanism`、`duration`（`min`）、`rates`、`anisotropy`（`0.0..1.0`）、`parameters`、可选 `model`（单步自定义模型名）。

| 工艺类型 | `kind` | `mechanism` | 必填字段与 `parameters` 写法 | 物理含义与引擎对齐说明 |
|---|---|---|---|---|
| 初始步（第 0 步） | `"initial"` | `"initial"` | `duration: 0` | 实例化 `initial` 中定义的衬底与初始薄膜栈，固定作为 `steps[0]` |
| 沟槽填充 + 盖层 | `"deposit"` | `"fill"` | `parameters: {"material": "SiO2", "offset": 0.05}` | 填平所有与顶面气相连通的凹槽，并在当前最高表面之上额外覆盖 `offset`（`um`）厚的平整层 |
| 保形（各向同性）沉积 | `"deposit"` | `"isotropic"` | `duration: 0.01`, `rates: {"TiN": 1.0}` | 沿所有暴露气固界面按欧氏距离保形生长 `rate * duration`（`um`） |
| 定向沉积 | `"deposit"` | `"anisotropic"` | `duration: 0.05`, `rates: {"SiO2": 1.0}`, `anisotropy: 0.8` | 按 `anisotropy` 拆分为纯垂向生长与保形生长分量 |
| 各向异性（定向）刻蚀 | `"etch"` | `"anisotropic"` | `duration: 0.16`, `rates: {"BARC": -1.0, "SiOC": -1.0}`, `anisotropy: 1.0`, 可选 `parameters: {"mask": ["PMMA"], "patterned": true}` | 自顶向下视线选择性刻蚀；遇到不在 `rates` 里的材料（掩膜或停止层）自动停止 |
| 各向同性（湿法/腔体）刻蚀 | `"etch"` | `"isotropic"` | `duration: 0.05`, `rates: {"Si3N4": -1.0}` | 从暴露气相向四周三维等速选择性掏空（支持 3D NAND 替换栅字线腔体） |
| 椭球锥角刻蚀 | `"etch"` | `"ellipsoidal"` | `duration: 0.05`, `parameters: {"rates": {"SiO2": [-1.0, -0.2]}, "mask": ["PMMA"]}` | `rates[mat]` 为 `[垂向速率, 侧向速率]` 二元组，用于带侧壁倾角（taper）的刻蚀 |
| 光刻图形化 | `"mask"` | `"litho"` | `parameters: {"layer": 11, "layer_name": "PLG", "thickness": 0.05, "coat": 0.1, "polarity": "negative", "material": "PMMA"}` | 读取 `job.layout.path` 指定层多边形；`negative` 表示多边形内开孔去胶、其余区域涂覆 `coat`（默认 `0.1 um`）厚 `PMMA` |
| 选择性灰化/剥离 | `"strip"` | `"strip"` | `parameters: {"materials": ["PMMA", "BARC"]}` | 将所列材料全部清除为 `void` |
| 化学机械抛光（CMP） | `"planarize"` | `"cmp"` | `parameters: {"stop_material": "SiO2", "offset": -0.001}`（或仅面向非 SPX 引擎用 `{"cutoff": 0.12}`） | 抛光至 `stop_material` 最高顶面 + `offset`（`offset < 0` 表示过抛蚀）；**与 SPX 对拍时必须用 `stop_material`** |
| 纯自定义模型步 | 任意合法 `kind` | `"custom"` | `model: "my_model_id"`, `parameters: {...}` | 专用于测试候选引擎独有的新物理模型（SPX 会将该步记为 `unsupported`） |

> **单步自定义模型覆写技巧**：如果你想让 **SPX 照常跑标准物理步骤**作为真值，同时让 **`voxel` 跑你刚写的新模型**做精度对比，保持 `kind` 和 `mechanism` 为标准值（例如 `"planarize"` + `"cmp"`），只需额外加一个 `"model": "cmp_dishing"` 字段！SPX 会忽略 `model` 按标准 `cmp` 执行，而 `voxel` 会优先路由到 `"cmp_dishing"`。

---

### 2.3 三种常用测试样例模板（可直接复制）

#### 模板 A：无需自建版图的多层选择性沉积/刻蚀测试（双引擎 / 三引擎通用）

> **SPX 窗口注意事项**：即使不跑 `litho`，SPX 底层仍要求 `clip` 落在默认版图（`Logic1_Layout.oasis`，范围 `[0.035, 0.0] .. [0.175, 0.288] µm`）内。因此无 `layout` 的测试请保持 `origin_xy: [0.035, 0.0]` 且 `width <= 0.14, depth <= 0.288`，或显式指定 `Memory1_Layout.oasis`（范围 `[0, 0] .. [0.624, 0.740] µm`）。

将以下内容保存为 `var/custom_stack.json`：

```json
{
  "schema_version": 1,
  "case_id": "custom-stack-test",
  "engine": "any",
  "domain": {
    "width": 0.12,
    "depth": 0.12,
    "height": 0.35,
    "grid_delta": 0.005,
    "dimensions": 3,
    "origin_xy": [0.035, 0.0]
  },
  "initial": {
    "kind": "plane",
    "substrate": "Si",
    "substrate_thickness": 0.10,
    "films": [
      {"material": "SiO2", "thickness": 0.06},
      {"material": "Si3N4", "thickness": 0.04}
    ]
  },
  "steps": [
    {
      "step_id": "s0_init",
      "kind": "initial",
      "mechanism": "initial"
    },
    {
      "step_id": "s1_partial_nitride_etch",
      "kind": "etch",
      "mechanism": "anisotropic",
      "duration": 0.025,
      "rates": {"Si3N4": -1.0, "SiO2": -0.05},
      "anisotropy": 1.0
    },
    {
      "step_id": "s2_ald_liner",
      "kind": "deposit",
      "mechanism": "isotropic",
      "duration": 0.008,
      "rates": {"HfO2": 1.0}
    }
  ]
}
```

#### 模板 B：带版图图形（光刻 + 刻蚀 + 大马士革填充 + CMP）测试

1. **方式 1（直接复用现成 SPX Memory1 版图与已知层映射，最稳妥，支持与 SPX 100% 跑通对拍）**：
   `Memory1_Layout.oasis` 覆盖 `[0.0, 0.0] .. [0.624, 0.740] µm`，已逐点标定 6 个光刻层：
   - `"M1": 9`、`"PLG": 11`、`"CS": 15`、`"BC": 17`、`"PC": 19`、`"M2": 21`。
   在 `JobSpec` 顶层加入：
   ```json
   "layout": {
     "path": "/root/SPX/spx/V-2024.03/linux64/examples/Memory1_Layout.oasis",
     "spxLayers": {"M1": 9, "PLG": 11, "CS": 15, "BC": 17, "PC": 19, "M2": 21}
   }
   ```
   并在 `litho` 步同时写 `"layer": 15`（供 `voxel`/`viennaps` 读多边形）和 `"layer_name": "CS"`（供 `spx` 按层名曝光）。

2. **方式 2（用 `gdstk` 生成任意自定义几何的 `.gds` 版图）**：
   适合专门构造极限几何（如不同深宽比沟槽阵列、密集孔洞、悬空桥结构）来压测 `voxel` / `viennaps` 或自定义模型：
   ```bash
   .venv/bin/python - <<'PY'
   import gdstk
   from engines import layout

   # 必须显式指定 1 um 用户单位 / 1 nm 数据库精度
   lib = gdstk.Library(unit=1e-6, precision=1e-9)
   cell = lib.new_cell("H1")
   # 在 [0.2 x 0.2 um] 窗口内画两条平行沟槽（层号 15）
   cell.add(gdstk.rectangle((0.03, 0.02), (0.08, 0.18), layer=15, datatype=0))
   cell.add(gdstk.rectangle((0.12, 0.02), (0.17, 0.18), layer=15, datatype=0))
   lib.write_gds("var/double_trench.gds")

   # 秒级检查生成的版图层号与包围盒（无需启动仿真引擎）
   print(layout.summarise("var/double_trench.gds").as_json())
   PY
   ```
   > **提示**：对于自建纯 `.gds` 文件，SPX 导入时默认层名为 GDS 层映射表名（若无层映射文件，推荐优先在 `voxel` 上做几何单测，或复用 `Memory1_Layout.oasis` / `Logic1_Layout.oasis` 与 SPX 对拍）。

#### 模板 C：从 SPX 导出的 Python 流程脚本自动翻译生成 `JobSpec`

拿到一份 SPX 导出的 `spx.procedural.*` 脚本时，不要手抄 JSON，直接调用 `engines.sources.spx_script.translate`：

```python
from pathlib import Path
from engines.protocol import write_json
from engines.sources import spx_script

script_text = Path("my_spx_exported_flow.py").read_text(encoding="utf-8")
job = spx_script.translate(
    script_text,
    case_id="my-flow-dx10",
    window=[0.0, 0.0, 0.624, 0.740],  # [x0, y0, x1, y1] (um)，面积须 <= 1.0 um^2
    grid_delta=0.01,
    layout_path="/root/SPX/spx/V-2024.03/linux64/examples/Memory1_Layout.oasis",
    spx_layers={"M1": 9, "PLG": 11, "CS": 15, "BC": 17, "PC": 19, "M2": 21},
    domain_height=2.5,
)
write_json(Path("var/my-flow-dx10.json"), job.to_json())
```

### 2.4 跑仿真前的秒级静态审计

写完 `JobSpec` JSON 后，**先跑静态审计**（检查漏剥胶、空层剥离、缺失 CMP 停止层、过刻穿底等逻辑错误，耗时 `< 0.1 s`）：

```bash
.venv/bin/python -m engines.audit var/custom_stack.json
```

---

## 3. 如何运行仿真、测速、诊断误差与预览 WebUI

### 3.1 CLI 常用命令速查

所有命令均在 `/root/wb-engines` 下执行：

```bash
# 1) 查看当前已注册发现的全部引擎、支持机制与安全限制
.venv/bin/python -m engines.cli engines

# 2) 纯求解速度基准测试（关闭出帧、checkpoint、探针与截面，串行零争用计时）
.venv/bin/python -m engines.cli run engines/examples/memory1-full-dx10.json \
  --engines voxel,spx --reference spx --benchmark --out var/bench_m1

# 3) 快速精度 + 速度联合对拍（跳过网格/截图渲染，保留 3000 点 3D 探针与 33 列截面）
.venv/bin/python -m engines.cli run engines/examples/memory1-full-dx10.json \
  --engines voxel,spx --reference spx --sequential --no-frames --no-checkpoints \
  --probe-points 3000 --out var/acc_m1

# 4) 生成完整 3D 渲染帧并自动接入 WebUI 预览（注意 --out 直接放在 var/<合法名称> 下！）
.venv/bin/python -m engines.cli run var/custom_stack.json \
  --engines voxel,viennaps,spx --reference spx --sequential \
  --probe-points 2000 --out var/custom_stack_run

# 5) 离线导出多步 2D 截面对比拼图 PNG（无需浏览器，适合终端快速目检几何）
.venv/bin/python -m engines.xsec \
  --grid var/custom_stack_run/engines/voxel/frames \
  --out var/custom_stack_xsec.png
```

### 3.2 在 WebUI 中实时预览对比结果

只要 `--out` 目录直接位于 `var/<run_name>`（且 `<run_name>` 仅含字母、数字、`_`、`-`，例如 `var/custom_stack_run`），后端 `GET /api/compare/runs` 会**自动采纳（adopt）**该目录，无需重启服务：

1. 打开 `http://127.0.0.1:8801/#compare`（若服务未运行，执行 `WORKBENCH_PORT=8801 .venv/bin/python backend/serve.py`）；
2. 在右侧**历史运行列表**点击 `custom_stack_run`；
3. 顶部 **步骤进度栏** 可一键跳转或用键盘 `←` / `→` 逐步切换；
4. 左右视口顶部下拉框可任意切换对比引擎（如左 `voxel` 3D 交互网格 ⇄ 右 `spx` 渲染图，或左 `voxel` ⇄ 右 `viennaps`）；
5. 底部双卡片实时展示当前步的**求解/出帧/探针分项耗时**与**总体一致率 / 有效材料一致率 / 材料 IoU**。

### 3.3 如何读懂 `compare.json` 并精准定位模型误差

运行完成后，`<out_dir>/compare.json` 包含全部量化指标。Agent 做模型调优时重点看这两组字段：

1. **速度指标（严禁混淆总进程时间与纯求解时间）**：
   - `engines["voxel"]["solveWallMs"]`：候选引擎所有步骤的**纯物理/几何求解核累计耗时**（不含写网格、探针、存盘）。
   - `engines["spx"]["solveWallMs"]`：SPX `sp.run()` 整条工艺批处理求解耗时。
   - `summary["speedupVsReference"]["voxel"]`：`ref_solve_ms / cand_solve_ms` 真实求解加速比。
   - `frameWallMs` / `probeWallMs`：出帧与探针采样开销（仅用于评估 I/O，不计入求解器速度对标）。

2. **精度指标（重点关注 `activeAgreement` 与 `topConfusions`）**：
   - `agreement`：全三维探针格一致率（包含衬底底部和顶部大量双 `void` 空气点，容易虚高）。
   - **`activeAgreement`（核心指标）**：剔除双 `void` 空气点后，所有真实材料区的逐点一致率。
   - **`featureAgreement`**：仅统计 `z >= 0` 特征器件层（剔除 `z < 0` 大块硅衬底与双 `void`）的一致率，对纳米薄膜与沟槽误差最敏感。
   - `topConfusions`：当前步最多的材料混淆对（例如 `[{"voxel": "PMMA", "spx": "void", "count": 14}]` 一眼看出是光刻胶开口圆角差异还是刻蚀深度差异）。
   - `section.materials[mat].meanAbsDiffUm`：33 列横截面上该材料顶面高度与 SPX 的平均绝对偏差（单位 `um`，`0.001` = `1 nm`）。

可以用下面这段单行诊断脚本快速找出整条流程里误差最大的前 5 步及原因：

```bash
.venv/bin/python - <<'PY'
import json
data = json.loads(open("var/m1_voxel_spx/compare.json").read())
rows = []
for s in data["steps"]:
    p = (s["comparisons"].get("voxel|spx") or {}).get("probe") or {}
    if p.get("activeAgreement") is not None:
        rows.append((p["activeAgreement"], s["index"], s["stepId"], p.get("topConfusions", [])[:2]))
for acc, idx, sid, conf in sorted(rows)[:5]:
    print(f"#{idx:02d} {sid:<32} active={acc:.4f}  confusions={conf}")
PY
```

---

## 4. 下一步引擎与工艺模型开发指南（三条演进路径）

本项目的终极目标是：**在保持与 SPX 等同精度的前提下，实现与 SPX 等同甚至数量级超越的计算速度**。
当前 `voxel` 引擎在 88 步 Memory1 上已达到 **`3.52 s` 纯求解（比 SPX `9.07 s` 快 `2.57x`）**、总体一致率 **`98.16%`**（非空材料一致率 **`93.66%`**）。下一步开发按目标分为三条清晰路径：

---

### 路径 1：针对具体工艺开发高精度单步模型（最快闭环，零侵入核心代码）

得益于 `engines/sdk/models.py` 的 `StepModelRegistry`，你**不需要修改引擎主框架**即可为任意特定工艺（如高精度光刻胶圆角、高深宽比接触孔 RIE lag、大马士革 CMP 碟形凹陷）编写独立模型并与 SPX 对拍。

#### 4.1.1 `VoxelState` 核心 API 速查（定义在 `engines/voxel/state.py`）

单步模型函数签名为 `(state: VoxelState, step: StepIR, ctx: StepModelContext) -> dict[str, Any] | None`：

- `state.grid`：`np.ndarray`，形状 `(nx, ny, nz)`，类型 `uint8`（C 连续）。`0` 恒代表 `void`（气相/真空），`1..254` 代表具体固体材料 ID。
- `state.dx, state.dy, state.dz`：体素尺寸（`um`）。`voxel` 采用各向异性网格：横向由 `job.domain.grid_delta` 决定，纵向默认加密至 **`dz ≈ 0.001 um`（`1 nm`）** 以精确解析 `2–5 nm` 超薄阻挡层。
- `state.material_id(name: str) -> int`：获取或分配标准材料名对应的整数 ID。
- `state.z_to_index(z_um: float) -> int`：物理高度 `z`（`um`）转 `grid` 第 3 维下标 `k`。
- `state.active_k_top() -> int`：当前非空材料的最高 `z` 上界下标（切片只需处理 `state.grid[:, :, :ktop]`，避免扫描上方空气）。
- `state.column_tops_k() -> np.ndarray`：返回 `int32[nx, ny]`，每列的最高非空下标。
- `state.exposed_void_mask(k_limit: int) -> np.ndarray`：返回 `bool[nx, ny, k_limit]`，标记所有与顶面气相连通的空腔体素（自动保护封闭气隙，同时连通替换栅侧向掏空腔体）。
- `state.rasterize_polygons(polygons) -> np.ndarray`：将局部坐标多边形光栅化为 `bool[nx, ny]` 掩膜。
- `dilate_3d_euclidean(mask, radius_um, dx, dy, dz) -> np.ndarray`：精确三通道可分离 3D 欧氏球膨胀（XY 周期边界）。
- `ctx.layer_polygons(layer: int)`：读取当前 `job.layout` 指定层号并自动减去 `origin_xy` 后的局部多边形列表。

#### 4.1.2 编写并挂载外部模型插件示例

新建任意文件（例如 `var/my_taper_etch.py`）：

```python
import numpy as np
from engines.voxel.models.etch import _top_down_column_etch


def register_models(registry):
    # 方式 A：注册命名模型（配合 StepIR 中的 "model": "rie_lag_etch_v1" 使用）
    @registry.register_model("rie_lag_etch_v1")
    def rie_lag_etch(state, step, ctx):
        rates = {
            state.material_id(mat): abs(float(r))
            for mat, r in step.rates.items()
            if float(r) < 0.0
        }
        removed = _top_down_column_etch(state, rates, float(step.duration))
        return {"removedVoxels": removed, "variant": "rie_lag_v1"}

    # 方式 B：直接覆写默认机制（例如接管所有 mask/litho 步骤）
    # @registry.register("mask", "litho")
    # def better_litho(state, step, ctx): ...
```

无需改动任何仓库文件，直接用环境变量 `VIENNAPS_STEP_MODEL_MODULES` 挂载并与 SPX 对拍：

```bash
VIENNAPS_STEP_MODEL_MODULES=$PWD/var/my_taper_etch.py \
  .venv/bin/python -m engines.cli run engines/examples/memory1-plug.json \
  --engines voxel,spx --reference spx --sequential --no-frames --out var/exp_plugin
```

验证达标后，再将模型合入 `engines/voxel/models/` 下的对应模块（`litho.py` / `etch.py` / `deposit.py` / `planar.py`）。

#### 4.1.3 当前最具价值的三个精度提升点（`98.16% -> >99.3%`）

1. **光刻胶高斯圆角（`engines/voxel/models/litho.py`）**：
   SPX `litho_patterning` 默认对 2D 掩膜做高斯模糊（`Sigma` 圆角化），而当前 `voxel` 直接按硬多边形边界光栅化（这是 Memory1 光刻步约 `1%–3%` 差异的主要来源）。对多边形指示函数做 2D 高斯卷积后再阈值化即可消除该差异。
2. **椭球刻蚀连续锥角（`engines/voxel/models/etch.py: etch_ellipsoidal`）**：
   当前实现采用“垂向推进 + 平均半宽横向扩孔”两步近似。改为按刻蚀深度线性插值侧向扩孔半径（顶部扩 `l_rate * duration`，刻蚀底面扩 `0`），即可精确贴合 SPX 锥形侧壁（如第 25 步 `25_etch_ellipso_oxide_taper`）。
3. **图形密度相关的 CMP 碟形凹陷与侵蚀（`engines/voxel/models/planar.py`）**：
   在已有 `@registry.register_model("cmp_dishing")` 基础上，按局部窗口内的金属线宽/密度卷积核计算碟形深度，可用于超越理想平面 CMP 的物理建模研究。

---

### 路径 2：算子级 C++ / CUDA / Warp 加速（把 88 步求解从 `3.5 s` 压到 `< 0.15 s`）

对 `engines/voxel` 在 Memory1 88 步上的耗时剖析表明，**95% 的求解耗时集中在以下 3 个纯数组原语**，且数据布局已经是规整的 `uint8[nx, ny, nz]`：

| 热点函数 | 所在文件 | 当前实现与瓶颈 | 推荐加速方案 |
|---|---|---|---|
| `dilate_3d_euclidean` | `engines/voxel/state.py` | Python `for k in range(nz)` 逐层递推 + `np.roll` 循环（占沉积与各向同性刻蚀 ~70% 时间） | 用 C++/OpenMP、PyBind11、Numba 或 CUDA 实现经典的 **Felzenszwalb-Huttenlocher 精确线性时间 `O(N)` 欧氏距离变换（EDT）** |
| `exposed_void_mask` | `engines/voxel/state.py` | 存在悬空腔体（如替换栅掏空后）时退化为 Python 循环的 6-邻域 `np.roll` 泛洪 | 用 C++ BFS 队列或 CUDA 并行连通分量（Connected Components）替换，单次耗时可从 `~40 ms` 降至 `< 0.3 ms` |
| `_top_down_column_etch` | `engines/voxel/models/etch.py` | 双重 Python 循环 `for ix in range(nx): for iy in range(ny):` 扫描每列 | 各 `(ix, iy)` 列**完全独立无数据依赖**！用 C++ OpenMP `collapse(2)` 或单个 CUDA kernel（每线程一列）可获得 `50x–200x` 加速 |

**算子替换注意事项**：
- XY 方向必须保持**周期边界条件**（`(ix + ox) % nx`），与 SPX `ColumnSizeX/Y` 周期单元及 ViennaPS 周期边界一致。
- `dilate_3d_euclidean` 的阈值比较需保留半格容差 `r_tol = radius_um + 0.45 * dz`（防止 `float32` 截断导致 `5 nm` 薄膜少长 `1 nm`）。
- 替换算子后，跑一遍 Memory1 对拍确认 `activeAgreement` 与替换前逐位一致（`>= 0.9366`），再用 `--benchmark` 验收加速比。

---

### 路径 3：接入一个全新的独立仿真引擎（C++ / CUDA / Taichi / 外部二进制）

如果你开发了不基于 `VoxelState` 的全新引擎（例如自适应八叉树、表面网格追踪或纯 C++/CUDA 可执行程序），平台支持**零框架改动**热插拔接入：

#### 方式 3A：基于 Python `EngineAdapterBase` 编写新引擎（推荐，约 60 行代码）

参考 `engines/voxel/engine.py`，继承 `engines.sdk.EngineAdapterBase[MyState]`，只需实现 5 个方法：
- `info() -> EngineInfo`
- `open_state(job, out_dir) -> MyState`
- `extract_frame(state, job, out_dir, step_id) -> Frame`
- `probe_points(state, points) -> list[str]`
- `section_heights(state, spec) -> dict[str, list[float | None]]`

框架会自动为你处理：分段微秒级计时（`solve_ms` / `frame_ms` / `probe_ms`）、`out_of_domain` 域守卫级联阻断、`--describe` 自描述输出、外部步骤模型加载与 `result.json` 原子写入。

#### 方式 3B：纯 C++ / Rust / CUDA 原生二进制引擎

任何可执行程序只要遵守以下命令行与文件协议即可作为引擎接入：
- 调用签名：`<binary> <job.json> <out_dir>`
- 输出文件：`<out_dir>/result.json`，结构如下：
  ```json
  {
    "engine": {
      "engine_id": "my_cuda",
      "version": "0.1.0",
      "dimensions": [3],
      "mechanisms": ["initial", "fill", "isotropic", "anisotropic", "cmp", "litho", "strip"],
      "frame_kinds": ["mesh"],
      "adapter": "my_cuda_bin",
      "solve_mode": "stepwise",
      "probe_method": "gpu_voxel"
    },
    "outcomes": [
      {
        "step_id": "s0_init",
        "status": "ok",
        "wall_ms": 0.4,
        "solve_ms": 0.4,
        "frame_ms": 1.2,
        "probe_ms": 0.1,
        "section_ms": 0.1,
        "frame": {
          "step_id": "s0_init",
          "kind": "mesh",
          "path": "frames/s0_init.mesh.json",
          "materials": [{"id": 1, "name": "Si", "color": "#78909C"}],
          "bbox": [0.0, 0.0, -0.3, 0.624, 0.74, 2.481]
        },
        "probe": ["Si", "void"],
        "section": {"Si": [0.0, 0.0]}
      }
    ],
    "run": {"solveMode": "stepwise", "solveMs": 0.4}
  }
  ```

#### 声明 `manifest.json` 完成注册

在仓库内新建 `engines/<engine_id>/manifest.json`（或放在任意外部目录并通过环境变量 `VIENNAPS_ENGINE_DIRS=/path/to/dir` 注入）：

```json
{
  "engine_id": "my_cuda",
  "label": "CUDA Fast Solver",
  "role": "candidate",
  "runtime": "venv_python",
  "entrypoint": "engine.py",
  "adapter": "engines.my_cuda.engine",
  "solve_mode": "stepwise",
  "probe_method": "gpu_voxel",
  "mechanisms": ["initial", "fill", "isotropic", "anisotropic", "ellipsoidal", "litho", "strip", "cmp", "custom"],
  "frame_kinds": ["mesh"],
  "limits": {
    "max_active_points": 50000000.0,
    "max_ratio": 5000.0,
    "max_area_um2": 50.0
  },
  "rationale": "GPU-accelerated 3D volumetric solver"
}
```
> `runtime` 可选值：`"venv_python"`（工作台 `.venv`）、`"spx_python"`、`"system_python"`、`"binary"`（原生可执行文件）。

保存后运行 `.venv/bin/python -m engines.cli engines` 即可看到新引擎，WebUI `/#compare` 的引擎选择器也会自动出现该引擎，无需改动任何前后端代码。

---

## 5. 提交前的回归验收清单

每次完成引擎或模型代码修改后，提交前必须按顺序跑通以下命令：

```bash
cd /root/wb-engines

# 1) 跑引擎层全部 66 项核心单元测试（必须用仓库内独立数据目录 + 8767 端口，避免抢占后台 WebUI 的 workbench.lock！）
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-run WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest \
  tests.test_engine_probe \
  tests.test_engine_layout \
  tests.test_engines \
  tests.test_compare_api \
  tests.test_multi_engine -v

# 2) 清理临时测试数据目录（严禁残留或提交 var/）
rm -rf var/test-run

# 3) 若改动了 frontend/，执行类型检查并重新构建静态产物
cd frontend && npm run typecheck && npm run build && cd ..

# 4) 检查空白字符与工作区干净度（严禁误提交 var/、.venv/、frontend/dist/）
git diff --check
git status --short
```
