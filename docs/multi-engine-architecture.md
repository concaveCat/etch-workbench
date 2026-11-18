# 多引擎对拍与可插拔单步工艺模型架构

本文档说明从“ViennaPS ↔ SPX 双引擎原型”升级为**通用 N 引擎速度/精度基准测试平台 + 单步工艺模型 SDK** 的设计与扩展方法。

> **面向自动化 Agent 的实操手册（如何构造自定义测试样例、运行仿真对拍、解读 `compare.json` 与开展下一步引擎开发）**：请直接阅读 [AGENT_ENGINE_DEV_GUIDE.md](./AGENT_ENGINE_DEV_GUIDE.md)。

---

## 1. 架构分层

1. **统一作业与测速契约（`engines/protocol.py`）**
   - `StepIR` 新增可选字段 `model: str | None`：允许针对具体工艺步骤指定自定义模型（如 `"cmp_dishing"`），参考引擎（SPX）继续消费标准 `(kind, mechanism)`，候选引擎优先路由到 `step.model`。
   - `StepOutcome` 严格拆分五项耗时：`solve_ms`（纯求解核耗时，批处理引擎如 SPX 为 `None`，总耗时记入 `run.solveMs`）、`frame_ms`、`probe_ms`、`section_ms`、`checkpoint_ms`。
   - `JobSpec` 新增 `emit_frames` 与 `emit_checkpoints` 开关，支持零渲染开销的纯速度基准对拍（`--benchmark`）。

2. **声明式多引擎注册与发现（`engines/registry.py` + `engines/*/manifest.json`）**
   - 自动扫描内置 `engines/*/manifest.json` 与环境变量 `VIENNAPS_ENGINE_DIRS` 指定的外部引擎目录。
   - 新增引擎无需改动调度器、限流器、后端 API 或前端页面。

3. **单步工艺模型 SDK（`engines/sdk/`）**
   - `StepModelRegistry`：用 `@registry.register(kind, mechanism)` 或 `@registry.register_model("custom_id")` 注册独立工艺模型，并支持通过 `VIENNAPS_STEP_MODEL_MODULES` 动态挂载外部 `.py` 插件。
   - `EngineAdapterBase`：统一封装域守卫（`out_of_domain` 级联阻断）、分段高精度计时、`--describe` 自描述输出与 `result.json` 落盘。

4. **N 引擎对齐调度与无稀释精度指标（`engines/scheduler.py`、`engines/probe.py`）**
   - 支持 `1..N` 个引擎同时运行，自动以 `role="reference"`（如 `spx`）或 `--reference` 为基准输出两两 `comparisons["<cand>|<ref>"]` 与加速比 `speedupVsReference`。
   - 新增 `activeAgreement`（剔除双 `void` 空气点）与 `featureAgreement`（`z >= 0` 特征层一致率）以及 `topConfusions` 混淆对统计。

---

## 2. 如何接入一个全新仿真引擎（例如 C++ / CUDA / Taichi 求解器）

只需新建目录（可在仓库内 `engines/my_gpu/`，或任意外部目录并通过 `VIENNAPS_ENGINE_DIRS=/path/to/dir` 注入），放入 `manifest.json`：

```json
{
  "engine_id": "my_gpu",
  "label": "My CUDA Process Solver",
  "role": "candidate",
  "runtime": "venv_python",
  "entrypoint": "engine.py",
  "solve_mode": "stepwise",
  "probe_method": "gpu_grid",
  "mechanisms": ["initial", "isotropic", "anisotropic", "cmp", "fill", "strip", "litho", "custom"],
  "frame_kinds": ["mesh"],
  "limits": {"max_active_points": 50000000.0, "max_ratio": 5000.0, "max_area_um2": 50.0}
}
```

随后直接运行三引擎同台对拍：

```bash
.venv/bin/python -m engines.cli run engines/examples/memory1-full-dx10.json \
  --engines my_gpu,voxel,spx --reference spx --sequential --out var/cmp3
```

---

## 3. 如何针对单个具体工艺快速编写自定义模型

无需修改任何引擎核心文件，只需新建一个独立 Python 文件（例如 `my_models.py`）：

```python
def register_models(registry):
    @registry.register_model("tapered_contact_etch_v2")
    def my_contact_etch(state, step, ctx):
        # 直接操作 VoxelState (state.grid [nx, ny, nz]) 或自定义状态
        ...
        return {"customNote": "applied tapered contact profile"}
```

在 `StepIR` 中将该步加上 `"model": "tapered_contact_etch_v2"`，运行时挂载：

```bash
VIENNAPS_STEP_MODEL_MODULES=/path/to/my_models.py \
  .venv/bin/python -m engines.cli run my_flow.json --engines voxel,spx --out var/exp1
```
