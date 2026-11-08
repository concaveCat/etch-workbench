# process_models v2.4 — 一页 quickstart（结构化输出 + 运行时基础）

v2.4 是当前工艺模型插件契约：把“工艺身份 / 实现 / 引擎 / 状态表示”拆开，并用一次
`resolve_execution()` 做准入、`ExecutionPlan.run()` 执行一步。v2.2 把 **flow state** 与
**report outputs** 分开：`execute()` 返回 `StepResult`，`StepResult.delta` 只描述下一步
要消费的状态，计量/产物走 `OutputSet`。v2.4 在 runtime/storage/conformance 之上完成
3C 清理：worker 单步 dispatch 只走 `ProcessSpec.parse_step`，插件包拆出 `step.py`，
ViennaPS helper 移到 `process_models/v2/engines/`。v1 已冻结，只作历史保留；完整设计与
边界见 [`docs/process-model-plugins-v2.md`](../../docs/process-model-plugins-v2.md)。

它仍是运行时基础；execution catalog 已通过 API 暴露，worker 有默认关闭的 v2 执行桥，
UI 尚未接入。见下面的
[Runtime / storage / conformance](#runtime--storage--conformance) 和
[`docs/plugin-architecture.md`](../../docs/plugin-architecture.md)。

## 分层（外加 resolver / conformance）

| 层 | 模块 | 职责 |
| --- | --- | --- |
| Runtime | `executor.py` + `storage.py` | 跨步骤 flow state、`mutates_input` checkpoint、逐步骤 `state`/`outputs`/plan identity 落盘；`FlowSession` 复用 run 级 registry/provider/backends 但保持 one-step 执行边界；worker 有默认关闭的 v2 执行桥（`VIENNAPS_WORKBENCH_V2_EXECUTION=1`） |
| Engine | `engine.py` + `engines/viennaps.py` + `engines/viennaps_cellset.py` | `EngineRequirement` / `EngineLease` / `StaticEngineProvider`，严格租用、不回退；ViennaPS helper 与 native cell-set 生命周期只依赖传入的 `ps`，不 import backend/worker |
| Geometry | `geometry/` | 一个共享 `GeometryEngine` 承载 `planarize` / `particleShape` / `boolean` / `gdsBoolean` / `removeMaterial`；每个 operation 是 `execution_kind="geometry"` 的 step spec |
| Service | `registry.py` + `resolver.py` + `process_models/execution.py` | 多版本 spec/variant 目录、`resolve_step` / `default_variant_id`、准入检查、产出 `ExecutionPlan`；`load_execution_registry()` 合并插件与内置 geometry specs，`dispatch_step()` 区分 process/field/geometry/legacy/invalid |
| State | `state.py` + `adapters.py` | `ProcessState{geometry, materials, layers, fields, voxels}`、`StateDelta`（只有 flow state）、`OutputSet`/`StepResult`、`prepare/commit` |
| Conformance | `conformance.py` + `output_schema.py` | `check_variant()` 契约级一致性检查；`output_contract_schema()` 输出描述 |

`resolver.py` 把 request 变成 plan：参数类型、维度、`reads`、`validate()`、引擎租约
都在这里检查；材料顺序由 `ProcessState.materials` 提供，不做重复校验。`ProcessState` 是 frozen，不含 engine/meta；engine 作为
`execute(..., engine)` 的显式参数传入。`ProcessSpec.parse_step(project, step)` 是唯一的
worker 单步 dispatch hook：返回参数模型表示认领，返回 `None` 表示不属于该工艺；
`ProcessRegistry.resolve_step()` 负责“恰好一个匹配”，`default_variant_id()` 决定未显式
指定 `variant_id` 时用哪个变体。

## Structured outputs（结构化输出）

- `StateDelta` = 下一步消费的 flow state：`geometry` / `materials` / `layers` /
  `fields`（`CellSetState`）/ `voxels`。没有 `artifacts` / `metadata` / `warnings`。
- `MaterialState` 是材料顺序的唯一来源；`LevelSetState` 只有 `handle` / `dimension` /
  `grid_delta`，不含材料顺序。`ProcessState.materials` 总是存在
  （`has("materials")` 恒为 true），`order` 可以为空。
- `OutputSet` = 带类型的报告输出：`metrology`（`MetrologyRecord`：metric/value/unit/
  region/method/uncertainty）+ `artifacts`（`Artifact`：kind/name/path/schema_version/
  metadata）。默认空。
- `StepResult` = `delta` + `outputs` + 少量 `metadata` + `warnings`。
- `metadata` 不是垃圾桶：只放实际速率、solver 配置这类本步 bookkeeping。一个带单位的
  数进 `MetrologyRecord`，一个文件进 `Artifact`，下一步要用的数组进 `Field` /
  `CellSetState`。

```python
StepResult(delta=StateDelta(geometry=..., materials=...), outputs=OutputSet(metrology=[...]), metadata=...)
```

FinFET 需要的 state 类型：`GridSpec` / `Field` / `CellSetState` 承载掺杂、损伤和
6/9 分量应力（`unit`、`association` = cell/point、`dtype`、`shape`、`components`）；
`Layer` / `LayerState` 承载 dummy gate、ILD、spacer 的层身份（`layer_id` 唯一，
`material_id` 可以重复）。`implant` / `anneal` / `analysis` 通过按需派生的 native
`DenseCellSet` 产生 cell-set 字段；`CellSetBackend` 只在 field 序列边界做
`ensure`/`snapshot`/`rehydrate`。当前没有 stress/device simulator、没有 UI schema/API。

## Runtime / storage / conformance

运行时基础切片新增四个模块：

| 模块 | 内容 |
| --- | --- |
| `storage.py` | `StateStore` / `StateStoreError`：逐步骤保存/读取 state、outputs、plan identity 和 `flow.json` |
| `executor.py` | `FlowStep` / `FlowRun` / `FlowExecutor` / `FlowSession`：多步执行、`mutates_input` checkpoint、engine lease 释放；`FlowSession` 复用 run 级对象但逐 step 执行与落盘 |
| `conformance.py` | `ConformanceReport` / `check_variant()`：契约级一致性检查 |
| `output_schema.py` | `output_contract_schema()`：`schemaVersion: 1` 的 JSON 可序列化契约描述 |

执行顺序：

```text
S_i -> resolve -> checkpoint (mutates_input) -> prepare -> execute
    -> commit -> S_{i+1} -> store
```

`FlowStep` 可额外 pin `expected_process_version` / `expected_variant_version`；pin 到未
注册版本直接报错。`FlowExecutor` 在每步结束（含异常）释放 engine lease。
`mutates_input=True` 的步骤会先复制 native geometry 再 resolve；从第二步起这是一次
冗余复制，但保留为**可接受的验证成本**：重新 resolve 必须校验变体真正要执行的副本
状态，去掉它会让 copy/alias 错误污染上游状态而不是在准入阶段失败。

落盘布局：

```text
<root>/
  flow.json
  steps/<step_id>/
    state.json      # schemaVersion 1：materials / geometry.ref / layers / field 元数据
    fields.npz      # field 序列边界一次；field_000, field_001, ...（可选 material_ids）
    outputs.json    # OutputSet：metrology + artifacts
    plan.json       # plan.identity_json()（camelCase）
    geometry.vpsd   # 可选，引擎暴露 Writer 时由 executor 写
```

`state.json` 由 `STATE_SCHEMA_VERSION` 管，`outputs.json` 由 `OUTPUTS_SCHEMA_VERSION`
管；两者独立，升一个不会让另一个失效。`identity()` 是内部 snake_case 形式，
`identity_json()` 是 `plan.json`/`flow.json` 使用的 camelCase JSON 边界。

边界：

- **有 catalog API，没有 UI**；`output_contract_schema()` 是输出契约，不改变项目
  schema，也不代表前端已消费 v2 run。
- **没有原生 geometry replay loader**：`state.json` 只记录 `geometry.ref`，重载需要
  调用方传 `geometry_loader`。
- **版本 pin 只覆盖 process/variant**：`expected_process_version` /
  `expected_variant_version` 已实现；params/输入 state/输出 artifact 的 hash 未记录，
  `implementation_hash` / engine fingerprint 只记录、不能重新 pin。
- **没有外部引擎 adapter**，也没有把 v2 run 接入 backend/worker 生产路径；worker 侧
  只有一个默认关闭的通用 plugin shadow/apply hook。

数据流细节、组件职责和剩余缺口见
[`docs/plugin-architecture.md`](../../docs/plugin-architecture.md)。测试结果见下面的
[怎么跑](#怎么跑)。

## 新增一个 process

最省事的方式是复制 [`docs/plugin-template/`](../../docs/plugin-template/)（它放在
`docs/` 下，不会被 `plugins/*/plugin.toml` 扫描）。

1. 建 `plugins/<id>/`，写 `plugin.toml`（`api` / `id` / `version` / `entrypoint`）。
2. 写 Pydantic 参数模型（`extra="forbid"`）。
3. 写 `step.py` 的 `parse_step(project, step) -> BaseModel | None`：不属于本工艺返回
   `None`，声称属于但字段缺失抛 `ValueError`。
4. 写 `ProcessSpec`（`id`、`version`、`label`、`params_model`、`dimensions`、
   `operations`、`default_variant`、`parse_step`）。
5. 写变体：`variant_id`、`version`、`capabilities`（需要时带 `output_kinds`）、`engine`、
   `implementation_hash`（`None` = registry 计算），以及 `validate(ctx, params)` 和
   `execute(state, params, ctx, engine) -> StepResult`。
6. 注册：`register_process(spec)` + `VariantRegistration(process_id=spec.id, variant=...)`。
   只传这两个字段，其余从变体派生。
7. 一步执行用 `run_step(...)`；需要先看 plan 时用 `resolve_execution(...)`，再 `plan.run()`。

```python
from process_models.v2 import ProcessSpec, VariantRegistration, run_step


def register_my_process(registry):
    spec = ProcessSpec(
        id="my_process",
        version="1.0.0",
        label="My process",
        params_model=MyParams,
        dimensions=(2, 3),
        operations=("etch",),
        default_variant="native",
        parse_step=parse_step,  # step.py: (project, step) -> MyParams | None
    )
    registry.register_process(spec)
    registry.register_variant(VariantRegistration(process_id=spec.id, variant=MyVariant()))


outcome = run_step(
    registry,
    provider,
    process_id="my_process",
    variant_id="native",
    params=MyParams(rate=1.0),
    context=ctx,
    state=state,
)
# outcome.plan.identity_json() 可直接写进 run 记录
# outcome.delta / outcome.outputs / outcome.metadata / outcome.warnings 来自 StepResult
```

完整可运行骨架（含 params/step/variant）见 v2 文档 §0.2 与
[`docs/plugin-template/`](../../docs/plugin-template/)。

## 怎么跑

不启动服务，使用隔离数据目录：

```bash
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-v2 WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest \
  tests.test_process_models_contract tests.test_voxel_adapter \
  tests.test_process_models_voxel_plugin \
  tests.test_process_models_v2_core tests.test_process_models_v2_execution \
  tests.test_process_models_v2_isotropic tests.test_process_models_v2_voxel \
  tests.test_v2_storage tests.test_v2_executor tests.test_v2_conformance \
  tests.test_v2_conformance_native tests.test_v2_output_schema \
  tests.test_plugin_lifecycle tests.test_worker_v2_isotropic \
  tests.test_worker_v2_isotropic_shadow tests.test_v2_isotropic_capability
```

v1 回归测试保持原样，也应在同一轮跑通。本轮已验证 **275 项定向测试通过**
（v1 38 + core/execution 99 + isotropic 20 + voxel 14 + lifecycle 25 + storage 19 +
executor 10 + conformance 12 + conformance native 1 + output schema 3 +
worker bridge 15 + shadow/apply 17 + capability 2），另加默认 isotropic dispatch
12 项通过。完整结果见 `docs/process-model-plugins-v2.md` §9.2。

## Worker shadow validation（通用 v2 plugin hook）

切换 worker 之前，先用默认关闭的影子校验证明 v2 插件复现现有 worker 路径。这是
**已落地**的集成切片，生产执行仍然走旧路径：

- 影子开关：`VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW=1`，不设则完全不参与，输出不变。
- 验证 apply 开关：`VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY=1`，只在 shadow 匹配后
  把 `ctx.domain` 换成 v2 domain；这是能力验证，不是生产 cutover。
- 分派是通用的：worker hook 不点名具体工艺，只用 `load_registry()` +
  `find_v2_process()`（内部 `registry.resolve_step(project, step)`）。没有插件认领时写
  `v2-plugin-shadow-skipped` 事件，不静默跳过。
- 每个插件的 `parse_step` 只认领自己的模型和受支持分支；例如 `isotropic` 只认领
  `model: isotropic`、`rateMode: uniform`、`isotropicMethod` 为 `levelSet` 的步骤，
  维度 2D/3D（测试实测 schema 5/7；`parse_step` 本身不检查 schema）。这是**插件自身**
  的范围，不是 worker hook 的特例。
- 机制：旧路径 apply 前深拷贝 domain → 老路径 apply → 在副本上跑
  `worker.process_models_v2.run_v2_step` → `compare_domains()` 比较
  `surface_state` digest 与 material map → 写 `v2-plugin-shadow` 事件（两侧 digest +
  plan identity）→ 一致后可选 apply 替换 domain 并写 `v2-plugin-applied`；不一致
  先写事件再抛错。
- process hook 不覆盖：`rateMode: material`、未迁移到 v2 的插件分支、默认生产 cutover。
  geometry hook 单独覆盖 `execution_kind="geometry"` 的 planarize/particleShape/boolean/
  gdsBoolean/removeMaterial。
  `rateMode: material` 和 geometric 步骤由 isotropic 的 `parse_step` 返回 `None`，
  因而只留下 skip 事件。

```bash
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-v2-shadow \
VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW=1 WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest -v tests.test_worker_v2_isotropic_shadow
```

bridge 本身的直接测试：`tests.test_worker_v2_isotropic`（不需要开关，也不启动服务）。
当前结果是 bridge 15 项、shadow/apply 17 项通过；`tests.test_v2_isotropic_capability`
2 项通过（2D 三步 + replay、3D 两步）。完整的 275 项结果见
`docs/process-model-plugins-v2.md` §9.2。

## 插件生命周期（manifest + lock）

插件不只是“代码里的一个类”，而是**可安装单元**：`plugins/<id>/plugin.toml` 声明
`api`/`id`/`version`/`entrypoint`，`plugins.lock` 固定实际安装的版本与字节哈希，
`load_registry()` 按 lock 发现并加载，任何漂移都直接失败（无回退）。项目 JSON 仍然
只写 `process_id`/`variant_id`/参数，**永远不会**出现模块路径或 callable。

```bash
.venv/bin/python scripts/lock-plugins.py            # 生成/重写 plugins.lock
.venv/bin/python scripts/lock-plugins.py --check    # 校验（0 = 一致，1 = 漂移）
```

- 发现根：`VIENNAPS_WORKBENCH_PLUGIN_DIRS`（`os.pathsep` 分隔），缺省 `<repo>/plugins`；
  lock 路径可用 `VIENNAPS_WORKBENCH_PLUGIN_LOCK` 覆盖。
- 改了 manifest 或实现代码就必须重新生成 lock，**不要手改**；改完要重启 worker/服务
  （没有热加载）。v2.4 改了 `isotropic` 的包布局（新增 `step.py`，引擎 helper 移入
  `process_models/v2/engines/`），实现哈希覆盖包内全部 `.py` 加上显式
  `implementation_files`，lock 已重新生成并通过 `--check`。
- replay 需要的插件版本没装是硬错误，不会用邻近/最新版本顶替。
- 当前 `plugins/` 注册 23 个 built-in 工艺插件（20 个 level-set 工艺 + `implant` /
  `anneal` / `analysis` 三个 field 插件，完整列表见 `docs/plugin-architecture.md` 状态行）；
  `VIENNAPS_WORKBENCH_V2_EXECUTION=1` 可在 worker 内启用 process/field/geometry 的 v2
  执行路径（默认仍走旧路径，便于逐段对比）；
  `voxel_probe` 是树内 proof 插件，没有 manifest，不在 `plugins/` 与 lock 里。
  GeometryEngine 的 operation 是 core 内置 spec，不占用 `plugins/`。

字段、哈希覆盖范围、失败场景与运维/开发清单见
[`docs/plugin-lifecycle.md`](../../docs/plugin-lifecycle.md)。

## 当前限制

- 未切换默认生产执行路径，不改项目 schema；项目暂时不能引用 v2.4 变体。
  仅有的 worker 接触点是上面的默认关闭通用 shadow/apply hook。`FlowExecutor` /
  `StateStore` 是独立运行时组件，没有 backend 集成。
- 没有 warm pool、引擎复用池、并发调度；`StaticEngine.release()` 是 no-op。
- engine plugin registry 已实现（`engine_plugins/*/engine.toml` + `engines.lock`），
  ViennaPS 本身也通过该入口加载；尚无外部二进制/远程引擎 adapter，也没有自动
  引擎/变体选择。
- `CellSetState` / `Field` / `LayerState` / `MetrologyRecord` / `Artifact` / `OutputSet`
  有类型、校验、adapter 写入和 `StateStore` 落盘。`implant` / `anneal` / `analysis`
  通过按需派生的 native `DenseCellSet` 产生 field；`CellSetBackend` 在 field 序列边界
  做 `ensure`/`snapshot`/`rehydrate`。
- field/layer 已能持久化到 `state.json` + `fields.npz`，但没有原生 geometry replay
  loader：`state.json` 只记 `geometry.ref`，重载要调用方提供 `geometry_loader`。
- 没有 params/输入 state/输出 artifact 的 hash，也没有可执行的
  `implementation_hash` / engine fingerprint pin；replay 目前只 pin process/variant
  版本。
- 没有 stress / device simulator；`Artifact(kind="device")` 只是报告类型。
- 已有 execution catalog API；前端和 run 查询还没有消费 v2 plan/output 记录。
- 纯 level-set 工艺（如 `isotropic`）返回空或少量 `OutputSet`；`analysis` 输出
  sheet-resistance/junction metrology，`csvProcess` 等按各自能力报告。`voxel_probe`
  仍是 proof，不产出 metrology/artifact。
- voxel 适配器只支持 height-field；fin/GDS cut-out 在 prepare 阶段被拒绝。
- `plugins/` 下 23 个 built-in 工艺插件由 manifest + lock 安装；GeometryEngine 的
  operation 是 core 内置 spec。voxel_probe（surrogate proof）仍在树内、没有 manifest，
  只能由代码显式注册。
  快照、VTK、预览、事件仍不在当前 executor 范围内；已落盘的是 state、outputs、
  plan identity 和 `flow.json`。
- conformance harness 只做契约级检查（`check_variant`），不做物理 parity；仍没有
  warm pool、API/UI、插件市场、不可信代码沙箱，也没有自动引擎/变体选择。
