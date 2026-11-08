# 插件组织与数据流

> 状态：v2.5 契约 + 生命周期 + isotropic/sf6o2/cf4o2/hbro2/sf6c4f8/fluorocarbon/ionBeam/faradayCage/wetEtch/teosDeposition/teospecvd/directional/selectiveEpitaxy/geometricTrenchDeposition/ald/oxidation/singleParticle/multiParticle/oxideRegrowth/csvProcess/implant/anneal/analysis + cell-set field 序列 + GeometryEngine（含 gdsBoolean）+ runtime/storage/conformance
> 基础已落地。本文描述**插件应该如何组织、数据如何流动**；这一片是运行时基础，
> 不是默认生产 cutover，也没有 UI；execution catalog 已通过 API 暴露。v2.5 在 v2.4 之上增加了
> 执行 profile（CPU/GPU backend + kernel）和引擎中立的状态 backend；v2.4 完成了
> 3C 清理：单步 dispatch 走 `ProcessSpec.parse_step`、插件包拆出 `step.py`、
> 引擎 helper 移到 `process_models/v2/engines/`。

## 1. 分层

| 层 | 代码 | 职责 |
| --- | --- | --- |
| Runtime / executor | `worker/`、`process_models/v2/executor.py`、`storage.py` | 跨步骤 flow state、checkpoint、on-disk state/outputs、plan identity、引擎策略；v2 runner 尚未接入 backend |
| Conformance | `process_models/v2/conformance.py`、`output_schema.py` | 变体一致性检查、可序列化的输出契约描述 |
| Engine | `process_models/v2/engine.py`、`engines/viennaps.py` | `EngineRequirement` / `EngineLease` / provider；ViennaPS 纯 Python helper（材料、速率、advection），不 import backend/worker |
| Service | `process_models/plugins/`、`registry.py`、`resolver.py`、`process_models/execution.py` | manifest、发现、lock、版本解析、`resolve_step` / `default_variant_id`、准入、`ExecutionPlan`；`load_execution_registry()` 合并锁定插件与内置 geometry specs，`DispatchDecision` 标明 process/field/geometry/legacy/invalid |
| State | `process_models/v2/state.py`、`adapters.py` | `ProcessState`、`StateDelta`、`StepResult`、`OutputSet`、表示转换 |
| Plugin | `plugins/<id>/` | 工艺参数、`parse_step`、spec、variant 实现、物理模型 |

原则：**plugin 只描述和执行工艺；runtime、持久化、快照、replay、UI 属于平台。**

步骤派发先做一次**纯判定**：`process_models/execution.py` 加载锁定插件并注册内置
geometry specs，`dispatch_step()` 返回 `process` / `field` / `geometry` / `legacy` /
`invalid`。没有 spec 认领是显式 `legacy`；解析失败或歧义是 `invalid`，不允许静默回退。
`ModelDescriptor.execution_kind` 让 API/UI 能区分三类 step。resolver 在引擎租约建立后还会从
typed params 的 `material` / `*Materials` / `depositMaterial` 等字段收集材料名，并用
native material registry 做早期校验；缺失材料在执行前失败，而不是到 native constructor 才失败。

## 2. 插件包布局

以 `isotropic` 为例：

```text
plugins/isotropic/
  plugin.toml              # api / id / version / entrypoint
  __init__.py              # 公开导出；外部包也可是顶层模块
  params.py                # canonical Pydantic params
  step.py                  # parse_step(project, step) -> params | None
  spec.py                  # ProcessSpec（default_variant + parse_step）
  variants/
    __init__.py
    native.py              # EngineVariant(kind=native)
  register.py              # register(registry)
```

外部插件可以是：

```text
plugins/my_process/
  plugin.toml              # entrypoint = "my_process:register"
  my_process/
    __init__.py
    params.py
    step.py
    spec.py
    variants.py
    register.py
```

loader 会把插件目录的父目录加入 `sys.path`，因此 manifest 里只写一个稳定的
`module:attribute`，不写文件路径。

### 2.1 职责边界

| 谁 | 负责 | 不负责 |
| --- | --- | --- |
| Plugin | params、spec、variants、reads/writes、物理模型、输出语义 | 快照、路径、seed、engine policy、全局状态 |
| Platform | manifest/lock、registry/resolver、engine lease、state adapter、executor、persistence、replay、API/UI | 具体工艺物理 |

### 2.2 一个插件至少包含

```python
# params.py
class MyParams(BaseModel): ...

# step.py
def parse_step(project, step):
    if step.get("model") != "my_process":
        return None
    return MyParams(...)          # 缺字段时抛 ValueError

# spec.py
MySpec = ProcessSpec(
    id="my_process", version="1.0.0", label="...",
    params_model=MyParams, dimensions=(2, 3), operations=("etch",),
    default_variant="native", parse_step=parse_step,
)

# variants/native.py
class MyNativeVariant(EngineVariant):
    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry"}),
        writes=frozenset({"geometry"}),
    )
    engine = EngineRequirement(kind="native", engine_id="viennaps", min_version="4.6.2")

    def validate(self, ctx, params): ...
    def execute(self, state, params, ctx, engine) -> StepResult: ...

# register.py
def register(registry):
    registry.register_process(MySpec)
    registry.register_variant(VariantRegistration(process_id=MySpec.id, variant=MyNativeVariant()))
```

## 3. 单步数据流

```text
Project step (process_id, variant_id, params)
        │
        ▼
resolve_execution
  → ExecutionPlan { spec, variant, engine lease, state adapter }
        │
        ▼
executor loads ProcessState S_i
  { geometry, materials, layers, fields, voxels }
        │
        ├─ variant.mutates_input?
        │     → checkpoint native geometry (copy `Domain`) before execute
        ▼
adapter.prepare(S_i, variant.reads)
  - voxels: derive from geometry
  - layers: must already be stored
  - fields: derived on demand from geometry by the engine's `CellSetBackend`
    (or rehydrated from a persisted snapshot)
        │
        ▼
variant.execute(S_i', params, ctx, engine_lease)
        │
        ▼
StepResult {
  delta: StateDelta { geometry, materials, layers, fields, voxels },
  outputs: OutputSet { metrology, artifacts },
  metadata, warnings
}
        │
        ▼
adapter.commit(S_i', result.delta, variant.writes)
        │
        ▼
ProcessState S_{i+1}
        │
        ├─ StateStore: state.json + fields.npz
        ├─ OutputSet: outputs.json
        ├─ plan identity: plan.json; flow.json 记录整个 run
        └─ optional native geometry: geometry.vpsd
        │
        ▼
next step uses S_{i+1}
```

一句话概括：`S_i -> resolve -> checkpoint (mutates_input) -> prepare -> execute ->
commit -> S_{i+1} -> store`。`FlowExecutor` 负责串起这条链；单步调用仍可直接用
`ExecutionPlan.run()` / `run_step()`，但不会自动落盘。

### 3.1 状态 vs 输出

| 类别 | 类型 | 是否进入下一步 |
| --- | --- | --- |
| flow state | `StateDelta` → `ProcessState` | **是** |
| report outputs | `OutputSet`（metrology/artifacts） | **否** |
| small metadata | `StepResult.metadata` | 否，只进 run record |

如果下一步需要某个输出值，必须把它提升为 `Field`/`Layer`/`MetrologyRecord` 引用，
而不是让下一个插件去读上一个插件的 `metadata`。

### 3.2 不可变性

- `ProcessState` 是 frozen；插件只返回 `StepResult.delta`（一个 `StateDelta`）。
- `MaterialState` 是材料顺序的唯一来源（`LevelSetState` 只有句柄和网格元数据）；
  `ProcessState.materials` 总是存在，`state.has("materials")` 恒为 true。
- `adapter.prepare/commit` 用 `dataclasses.replace` 生成新状态，不原地改输入。
- `mutates_input=True` 的 native 变体（如 isotropic）会原地改 native geometry handle；
  `FlowExecutor` 在调用前用 `engine.handle.Domain(state.geometry.handle)` 复制一份
  geometry，再用副本重新 resolve，保证 `plan.request.state` 指向副本。引擎不暴露
  `Domain` 时直接抛 `ExecutionResolutionError`，不做“假定可重入”的降级。

### 3.3 运行时组件

| 组件 | 模块 | 职责 |
| --- | --- | --- |
| `StateStore` | `process_models/v2/storage.py` | 逐步骤保存/读取 `ProcessState`、`OutputSet`、plan identity 与 `flow.json`；原子 JSON 写（`tmp` + `os.replace`） |
| `FlowExecutor` | `process_models/v2/executor.py` | 消费 `FlowStep` 列表，执行 `S_i -> resolve -> checkpoint -> prepare -> execute -> commit -> S_{i+1}`，逐步骤落盘并返回 `FlowRun`；支持 `expected_process_version` / `expected_variant_version` 严格 pin，并在每步结束（含异常）释放 engine lease |
| `check_variant` / `ConformanceReport` | `process_models/v2/conformance.py` | 对单个变体做契约级一致性检查（解析、校验、`StepResult` 形状、输入不可变、strict determinism） |
| `output_contract_schema` | `process_models/v2/output_schema.py` | 返回 `StepResult`/`StateDelta`/`OutputSet` 等类型的 JSON 可序列化契约描述；是给未来 API/UI 的 schema，不是完整 JSON Schema 校验器 |

`FlowExecutor.run()` 的输入是 frozen 的 `FlowStep`（`step_id`、`process_id`、
`variant_id`、`params`、`context`，可选两个版本 pin）和初始 `ProcessState`；输出是
`FlowRun`：

```python
@dataclass(frozen=True)
class FlowRun:
    run_dir: Path
    outcomes: tuple[ExecutionOutcome, ...]
    state: ProcessState
```

### 3.4 落盘布局

```text
<root>/
  flow.json                    # 调用方提供的 run 描述（run id、steps、versions）
  steps/<step_id>/
    state.json                 # schemaVersion 1：materials / geometry / layers / fields 元数据
    fields.npz                 # 仅在有 fields/cell-set 时写；field_000, field_001, ... 按 JSON 顺序（可选 material_ids）
    outputs.json               # OutputSet：metrology + artifacts
    plan.json                  # plan.identity_json()（camelCase）
    geometry.vpsd              # 可选；引擎暴露 Writer 时由 FlowExecutor 写
```

规则：

- Cell-set 是**按需派生的辅助场域**，不是第二套主状态：`ProcessState.geometry`
  始终是 canonical level set；连续的 `implant -> anneal -> analysis` 自动组成一个
  field 序列。
- field 序列开始时 `CellSetBackend.ensure` 生成一次 native `DenseCellSet`，
  后续步骤复用同一 handle；`snapshot` 只在序列边界执行一次，`rehydrate` 只在
  replay/从磁盘恢复时执行。中间步骤不复制 cell set、不写 `fields.npz`、不写
  native cell-set 文件。
- 同一序列内 `height` / `substrateDepth` 在 route 编译阶段取最大值，
  `coverMaterial` / `periodic` 必须一致；`sourceGeometryDigest` 不匹配时 fail
  closed，不自动 remap。
- Geometry step 也是普通 step spec（`execution_kind="geometry"`），复用同一个
  registry / resolver / executor / state backend / persistence，但不是物理工艺：
  `planarize`（显式 cutoff 或 stopMaterial 测量）、`particleShape`、`boolean`、
  `gdsBoolean`、`removeMaterial` 由一个共享的 `GeometryEngine` 实现，每个 operation
  有自己的 params，且只操作 Domain/level set。GDS 仍是 asset：`gdsBoolean` 通过
  `ProcessContext.assets["gdsBooleanOperand"]` 取得已验证的 GDS operand builder，
  不把 GDS 解析塞进几何引擎。
- `fields.npz` 以 `allow_pickle=False` 读取；`Field.data` 必须是 numpy 数组，opaque
  handle 直接报 `StateStoreError`。
- `outputs.json` 拒绝 NaN/Inf 和不可序列化值，报 `StateStoreError`。
- `voxels` 是逐步骤 scratch，**不落盘**；下一步需要时由 adapter 从 geometry 重新派生。
- `state.json` 的 geometry 只记录 `geometry.ref`（通常是 `geometry.vpsd`）和维度/
  `gridDelta`；材料顺序在顶层 `materials`。`StateStore.load_state()` 不内置原生几何
  replay loader：有 ref 但没有调用方传入的 `geometry_loader` 时直接报错，不做静默降级。
- `state.json` 由 `STATE_SCHEMA_VERSION` 管，`outputs.json` 由 `OUTPUTS_SCHEMA_VERSION`
  管；两者独立，升一个不会让另一个失效。
- `plan.json` / `flow.json` 里的 plan identity 用 `identity_json()` 的 camelCase 键；
  `identity()` 是内部 snake_case 形式，不直接落盘。

### 3.5 Conformance 与输出契约

`check_variant(...)` 检查的是契约，不是物理精度：

- registry 能解析 `process_id` / `variant_id`，参数类型匹配 `ProcessSpec.params_model`；
- `variant.validate()` 返回的 error 级 `Issue` 会让检查失败；
- `execute_plan()` 必须返回带 `StateDelta` 和 `OutputSet` 的 `StepResult`；
- `mutates_input=False` 时，输入 state 的 geometry/layers/fields handle 在 execute 后
  身份不变；
- 声明 `determinism="strict"` 时，两次从 `state_factory()` 出发的运行产生相等的
  state delta / outputs（可传 `digest` 回调；handle 不可比较时退化为 metadata/outputs
  比较）。

`output_contract_schema()` 给出同一组类型的 `schemaVersion: 1` 描述，供未来 API/UI
复用；当前没有 HTTP API 或前端字段消费它。

### 3.6 执行 profile 与状态 backend

执行侧和状态侧是两个正交边界：

| 概念 | 取值/接口 | 归属 |
| --- | --- | --- |
| engine | `viennaps` / 自创引擎 | `EngineRequirement.engine_id` |
| backend | `cpu` / `gpu` | `ExecutionProfile.backend` |
| kernel | ViennaPS: `CPU_DISK`/`GPU_DISK`/`CPU_TRIANGLE`/`GPU_TRIANGLE`；自创引擎自定义 | `ExecutionProfile.kernel` |
| device | GPU UUID / CPU 线程池 | 平台解析，不由插件声明 |
| strategy | multi-ion split/merge 等 | `ExecutionProfile.strategy` |

插件只声明自己支持的 profile：

```python
engine = EngineRequirement(
    kind="native", engine_id="viennaps", min_version="4.6.2",
    profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
)
```

平台把 `solver.enginePreference` / `stepEngines` 解析成 `ComputePolicy`，再选出
`ExecutionProfile`；插件在 `execute` 里调用 `apply_execution_profile(ps, process,
engine.profile)`，不再写死 `CPU_DISK`。worker bridge 默认提供 `CPU_DISK` /
`CPU_TRIANGLE`；GPU 路径需要显式 `VIENNAPS_WORKBENCH_V2_PLUGIN_GPU=1`，并要求
worker 已选择 CUDA 设备并完成 readback。`GPU_TRIANGLE` 继续走 capability evidence
gate；不能执行的步骤记录 `v2-plugin-shadow-skipped`，不会静默降级到 CPU。

状态侧：

- `StateProfile` 是插件声明（允许的转换、必需材料、是否允许新材料、persistence）；
- `StateContract` 是平台解析出的具体契约（grid、材料、转换策略、backend id/version），
  进入 `ExecutionPlan.identity()`；
- `StateBackend` 是引擎实现（`checkpoint` / `write` / `load` / `validate` / 材料 registry）。
  `FlowExecutor` 不再直接调用 `Domain`/`Writer`，而是调用 `plan.engine.state_backend`。
- 内置 `HandleStateBackend` 兼容暴露 `Domain`/`Writer`/`Reader` 的引擎；自创引擎实现
  自己的 `StateBackend`，executor 和插件协议都不需要改。
- `MaterialRegistry` 统一 name ↔ native id；worker bridge 使用
  `ViennaPSMaterialRegistry`，`StateProfile.required_materials` 在 resolve 阶段校验。
- `sf6o2` 声明 `CPU_DISK`、`CPU_TRIANGLE`、`GPU_DISK`、`GPU_TRIANGLE` 四个 profile；
  GPU 路径由 `VIENNAPS_WORKBENCH_V2_PLUGIN_GPU=1` 显式打开，GPU_TRIANGLE 仍要求证据行。

当前状态 backend 只覆盖 level set + materials 的 copy/write/load/validate；voxel
转换仍在 adapter，cell-set / surface field / layer / GDS 按后续批次增加。

## 4. 三类插件的数据流

### 4.1 native

```text
ProcessState.geometry.handle + EngineLease.handle (ps)
        → native model / Process
        → geometry delta (+ optional layers/fields)
```

- 当前：`isotropic`（2D/3D，uniform/material level-set，mask，StepResult）；
  `sf6o2` / `cf4o2` / `hbro2` / `sf6c4f8`（flat plasma etch + CoverageParameters）；
  `fluorocarbon`（完整 FluorocarbonParameters + material table）、`ionBeam`（IBEParameters + masks）、
  `faradayCage`（FaradayCageParameters + masks，CPU_DISK only）；
  `wetEtch`（materialRates / crystalline）；`teosDeposition` / `teospecvd`（SiO2 top level set）；
  `directional`（scalar / rateSets / materialRates）；`selectiveEpitaxy`（SiGe）；
  `geometricTrenchDeposition`（几何 advection，duration 不缩放厚度）；
  `ald`（cycles；surfaceDiffusion 暂留旧路径）、`oxidation`（无 mask 基础路径）、
  `singleParticle`（uniform/material + deposit）；`multiParticle`（CPU profiles，GPU split 暂缓）、
  `oxideRegrowth`（原生 DenseCellSet + ignoreVoids）、`csvProcess`（content-addressed CSV，
  通过 `ProcessContext.assets` 提供已校验副本）。
- 引擎：ViennaPS；CPU/GPU/kernel 由 `EngineRequirement.profiles` + 平台 `ComputePolicy` 解析。

### 4.2 surrogate

```text
ProcessState { geometry, voxels, fields, layers } + params
        → pure Python/NumPy model
        → typed StateDelta / OutputSet
```

- 当前：`voxel_probe`（proof；height-field identity/erode）。
- 表示转换由 adapter 负责；surrogate 不直接持有 `ps`。

### 4.3 external

```text
stable input package (mesh/voxel/field files or arrays)
        → external engine/binary
        → stable output package
        → platform converts to StateDelta / OutputSet
```

- 当前：`external` 可由 `engine_plugins/*/engine.toml` 提供的 `EngineProvider` 实现；
  `engine-registry` 已支持发现、lock、版本/实现哈希和 provider 组合。尚无远程/子进程
  外部二进制的内置 adapter。
- 还需要：进程隔离、输入/输出 package schema、超时/资源上限。

## 5. 多步 flow 与 replay

```text
S_0 → step_1 → S_1 → step_2 → S_2 → ... → S_n
```

- 已实现：`FlowExecutor` 把每个 step 的 `plan.identity_json()` 写进
  `steps/<step_id>/plan.json`，并在 `flow.json` 里记录本次 run 的步骤顺序与版本；
  `StateStore` 固定 `state.json` / `fields.npz` / `outputs.json` 的 schema。
- 已实现：`ExecutionRequest` 与 `FlowStep` 的 `expected_process_version` /
  `expected_variant_version` 是当前唯一可执行的 replay/版本 pin；pin 到未安装版本直接
  报错，不会回退到最新版本。
- **未实现**：输入状态 hash、params hash、输出 artifact hash 都没有进入 run record；
  `implementation_hash` 与 engine fingerprint 只被记录，不能作为 pin 重新校验；
  `plugins.lock` 也不写进 plan identity。当前 replay 只能保证“同 process/variant 版本”，
  不能声称字节级可复现。
- 原生几何 replay 还缺 loader：`state.json` 只有 `geometry.ref`，重载需要调用方提供能把
  ref 变回引擎 handle 的 `geometry_loader`；store 本身不生成/解析 `.vpsd`。
- 插件升级 = 新 variant 版本或新 process 版本；旧 lock 仍可加载旧版本（如果安装）。

## 6. 当前实现状态

| 项 | 状态 |
| --- | --- |
| manifest / discovery / lock | ✅ |
| `ProcessSpec`（含 `parse_step` / `default_variant`）/ `VariantRegistration` / resolver / `ExecutionPlan` | ✅ |
| `ProcessRegistry.resolve_step` / `default_variant_id` | ✅ 单步 dispatch 只走 spec 的 `parse_step` |
| `ProcessState` / `StateDelta` / `StepResult` / `OutputSet` | ✅ 类型与校验；`StepResult.delta` |
| native isotropic 插件（`StepResult.delta`） | ✅ |
| voxel proof surrogate（`StepResult.delta`） | ✅ |
| 标准插件包布局（isotropic 含 `step.py` / `parse_step`） | ✅ |
| 可复制脚手架 `docs/plugin-template/` | ✅ 不在 `plugins/*` 扫描范围 |
| field/layer store + persistence | ✅ `StateStore`：`state.json` + `fields.npz`，原子写，geometry 只存 ref；field 序列只在边界写一次 `fields.npz` |
| 通用 executor 消费 `StepResult`/`OutputSet` | ✅ `FlowExecutor`：多步状态转移、`mutates_input` checkpoint、逐 step 落盘；**尚未接入 backend** |
| `OutputSet` 落盘 | ✅ `outputs.json`（metrology/artifacts，拒绝 NaN/Inf） |
| `OutputSet` API / UI | ❌ 只有 Python 契约和 `output_contract_schema()` |
| conformance harness | ✅ `check_variant()`：解析、校验、`StepResult` 形状、输入不可变、strict determinism |
| output contract schema | ✅ `output_contract_schema()`，JSON 可序列化，`schemaVersion: 1`；不是完整 JSON Schema 校验器 |
| generic worker plugin hook | ✅ 默认关闭的 process + geometry shadow/apply；事件 `v2-plugin-shadow` / `v2-plugin-applied` / `v2-plugin-shadow-skipped`；生产默认路径不变 |
| opt-in v2 execution | ✅ `VIENNAPS_WORKBENCH_V2_EXECUTION=1`：process/field/geometry 按 dispatch 走 v2，`WorkerV2State` 跨 step 保留 geometry/materials/cell-set；legacy fallback 会失效 field 历史并 fail closed |
| external engine adapter | ❌ |
| 原生 geometry replay loader | ❌ `geometry.ref` 已记录；重载需调用方提供 loader |
| v2 run 接入 backend/API/UI | ❌ |
| 定向测试 | 275 项通过 + 默认 isotropic dispatch 12 项通过（具体见 v2 文档 §9.2） |

### 6.1 当前仍未完成

- **有 catalog API，没有 UI**：`output_contract_schema()` 只是契约描述，项目 JSON 和
  前端还没有消费 v2 run。
- **没有原生几何 replay loader**：`StateStore` 记录 `geometry.ref`（以及可选的
  `geometry.vpsd`），但把 ref 重新加载成引擎 handle 的 loader 由调用方提供；
  缺失时 `load_state()` 直接报错。
- **没有外部引擎 adapter**：`external` 仍只是枚举值，registry/provider 只支持 ViennaPS
  原生路径。
- **没有 backend 集成**：`FlowExecutor` 是独立运行时组件；worker/正式服务仍走旧路径。
  worker 侧只有一个通用的、默认关闭的 process/geometry shadow/apply hook（按
  `ProcessSpec.parse_step` 分派，v2 文档 §7.5），没有 isotropic 专用 bridge，也没有生产
  cutover。

## 7. 新增插件的清单

1. 复制 `docs/plugin-template/` 到 `plugins/<id>/`（模板不在 `plugins/*` 扫描范围）。
2. 改 `plugin.toml`：`api` / `id` / `version` / `entrypoint`。
3. `params.py`：canonical Pydantic 参数，`extra="forbid"`。
4. `step.py`：`parse_step(project, step) -> BaseModel | None`；不属于本工艺返回 `None`，
   声称属于但字段缺失抛 `ValueError`。
5. `spec.py`：`ProcessSpec`，显式写 `default_variant="..."` 和 `parse_step=parse_step`；
   版本只随 schema/语义变。
6. `variants/`：每个实现一个 `EngineVariant`，声明 `reads/writes`、engine requirement、
   `mutates_input`、`output_kinds`；`execute()` 返回 `StepResult(delta=...)`。
7. `register.py`：只注册 spec + `VariantRegistration(process_id, variant)`。
8. 写 parity/conformance 测试；至少跑一次 `check_variant(...)`，native 变体还必须与
   worker 路径对照。
9. 运行 `python scripts/lock-plugins.py` 生成 lock，`--check` 通过。
10. 不要在插件里做快照、文件路径、seed、engine policy 或全局状态。
