# 工艺模型插件架构（设计草案）

> 状态：**v1 已冻结（frozen legacy）**。本文只保留为 v1 第一切片的历史记录，
> **不再更新，也不要在新代码里继续使用 v1 API**。当前落地的是
> `process_models/contract.py`、`registry.py`、`voxel.py`（height-field 适配器）
> 以及一个实验性 `voxel_probe` proof plugin；v1 **尚未**接入 backend/worker，
> 也没有 legacy 包装、真实工艺插件或 API/UI。
>
> **目标实现是 v2.1，见 [`process-model-plugins-v2.md`](process-model-plugins-v2.md)。**
> v1 的 `ProcessPlugin` 已由 v2.1 的 `ProcessSpec` + `VariantRegistration` 取代，
> 原生句柄改为显式 `EngineLease`，状态改为多表示 `ProcessState`/`StateDelta`，
> 执行入口是 `resolve_execution()`、`ExecutionPlan.run()` / `execute_plan(plan)`
> 和 `run_step()`。**v2.1 才是目标；v1 仅是历史。** 下文描述 v1 契约与当时的
> 迁移顺序，其中的 API 不再演进；保留原文是为了记录设计演进，不是使用指南。

## 1. 为什么现在需要拆

工艺模型今天集中在这五处，而且每一处都同时承担多种职责：

| 文件 | 位置 | 今天的职责 |
| --- | --- | --- |
| `backend/specialized_models.py` | `CATALOG`（672）、`model_spec()`（809） | 原生 allowlist、默认参数、UI 字段描述、部分校验规则的唯一来源 |
| `backend/models.py` | `SpecializedStepV7`（1226） | `model: "specialized"` 的 schema；形状校验直接调用 `model_spec()` |
| `backend/validation.py` | `_validate_specialized_steps()`（1149） | 逐模型语义校验、维度/材料/引擎 gate、警告 |
| `worker/specialized.py` | `build_specialized_model()`（194） | 按 `nativeModel` 构造原生对象，`if/elif` 覆盖每个模型 |
| `worker/steps/process.py` | `run_process_step()`（28）、specialized 分支（216） | 步骤分流、引擎/种子选择、快照与产物落盘 |

集中本身不是错误：它让“提交的 JSON 不能选择任意 Python 属性或构造器”这条安全边界
一直成立。问题是**新增一个单元工艺要同时改五个文件**，而且原生实现、schema、UI
描述、校验规则和 replay 身份混在同一层：

- `nativeModel` 是应用内 allowlist 键，不是稳定的插件身份；没有版本，也没有实现哈希。
- 只有 native ViennaPS 一条实现路径；无法在同一流程里按步骤换实现。
- replay 只固定 `viennapsVersion` / `viennalsVersion` / 引擎指纹（`ReplayRequirements`，
  `backend/models.py:2272`），不固定“哪个模型实现”。
- UI 字段、校验和 worker 构造共用一份 `CATALOG`，加模型时容易只改一半。

## 2. 两级模型

### 2.1 `ProcessPlugin`：稳定的单元工艺

一个 `ProcessPlugin` 表示“这是什么工艺”，不表示“怎么算”。它负责：

- **稳定身份**：`pluginId`、`pluginVersion`。项目 JSON 只引用这两个值。契约代码里
  对应 `ProcessPlugin.id` / `ProcessPlugin.version`；replay 字段加前缀以免与变体混淆。
- **规范参数 schema**：字段名、类型、单位、默认值、范围、互斥关系。
- **校验**：只校验参数与工艺语义，不接触文件系统、快照或引擎策略。
- **UI 元数据**：label、单位、枚举项、分组、字段说明；前端只读这份描述。
- **输出语义**：输出表面（surface）与材料 ID 的约定，而不是具体文件名。
- **变体表**：声明 `defaultVariant` 与可用 `variants`。

### 2.2 `EngineVariant`：可替换的实现

一个 `EngineVariant` 表示“这个工艺由谁执行、执行到什么程度”：

| 字段 | 含义 |
| --- | --- |
| `variantId` / `variantVersion` | 稳定引用键；项目与 replay 只用这两个值 |
| `kind` | `native`（ViennaPS）/ `surrogate`（Python 模型）/ `external`（外部引擎） |
| `implementationHash` | 实现文件（native 为 wrapper + 参数映射；Python 为源码包）的 SHA-256 |
| `engineRequirement` | 需要的引擎与最低版本；`native` 必须声明 ViennaPS/ViennaLS 版本 |
| `determinism` | `strict`（同种子逐字节一致）/ `tolerance`（给出容差与度量） |
| `resourceBounds` | 最大网格、墙钟、RSS；由 executor 在执行前检查 |
| `conformance` | 最近一次通过的一致性记录 ID；没有记录就不能注册为可用 |

### 2.3 选择规则

- 每个单元工艺默认使用 **native ViennaPS 实现**；`defaultVariant` 必须指向一个
  `kind: "native"` 的变体。
- 变体是**逐步骤**可选的：同一条 flow 里第 1 步用 native、第 4 步用 surrogate 是
  合法配置。选择只影响该步骤，不改变 flow state 的表示。
- 选择是显式的，不做“native 不可用就换 surrogate”的自动降级（见 §4）。

## 3. 阶段 1 的域：只做 level-set

阶段 1 的 flow state 始终是 **level-set domain**。voxel 不是第二种 flow state，而是
某个步骤内部的一次性适配（选项 1.a）：

```
level-set domain
   └─(步骤内) level-set -> voxel 栅格 -> 模型计算 -> level-set 回写
level-set domain（下一步继续使用）
```

要求与边界：

- 适配器只在该步骤内存在；voxel 栅格不进入快照、不进入 replay、不跨步骤保存。
- 回写必须产生合法的 level-set domain（重新初始化/重采样后的符号距离场），并保持
  材料 ID 映射；不能把 voxel 掩码直接当成 level-set。
- 第一切片已用 `viennals.ToVoxelMesh` 读取、边界网格 + `FromSurfaceMesh` 回写，
  跑通 2D/3D plane、trench、thin-film 往返；只支持 **height-field** 几何，
  fin/GDS 会在读取时拒绝。详见 §15。
- **阶段 1 必须做出一个可运行的最小转换 spike**，不是只写类型或接口：
  - 2D 与 3D 各一个固定 fixture，能完成 `level-set -> voxel -> level-set` 往返；
  - 往返后的表面在声明容差内一致（建议先以 `gridDelta` 为量纲定义，具体阈值待定）；
  - 同输入重复运行结果一致；
  - spike 的结果写入一致性记录，但**不**声称任何 voxel 物理模型的精度。
- 该 spike 只证明“表示转换 + 回写”可用。真正的 voxel-native 流程是阶段 1 的非目标。

## 4. 安装、身份与失败语义

Python 插件由**运维安装**，不是用户上传：

- 插件放在只读插件目录（部署时确定，例如 `VIENNAPS_WORKBENCH_PLUGIN_DIR`），目录内容
  不随项目 JSON 变化。
- 每个插件包带 `manifest.json`：`pluginId`、`pluginVersion`、`variantId`、
  `variantVersion`、`implementationHash`、声明的引擎要求。
- 注册表在服务启动/worker 启动时扫描并校验哈希；安装后修改文件视为不同实现。
- 项目 JSON 只允许出现稳定 ID：
  ```json
  { "model": "specialized", "nativeModel": "sf6o2",
    "variant": { "pluginId": "plasma-etch.sf6o2", "variantId": "native-viennaps" } }
  ```
  不允许路径、模块名、函数名、类名或可调用对象。`nativeModel` 是历史字段，迁移期
  由 registry 映射到 `pluginId`；新字段稳定后再决定是否弃用。

失败一律显式，**没有静默回退**：

| 情况 | 结果 |
| --- | --- |
| 未知 `pluginId` / `variantId` | 422 |
| 插件未安装，或缺少请求的 `pluginVersion` / `variantVersion` | 422 |
| 已安装文件的 `implementationHash` 与请求/replay 不符 | 422 |
| 变体不支持该步骤的维度、operation 或几何类型 | 422 |
| 参数不符合插件 schema | 422 |
| native 变体不可用（版本不符、引擎缺失） | 422；**不**自动改用 surrogate |

版本解析规则：

- 请求显式写了 `pluginVersion` / `variantVersion` 时，必须精确命中；缺失即 422。
- 请求省略版本时，可以解析到已注册版本中唯一的最高语义版本；解析结果必须写入
  `actualParameters` 与运行记录。
- replay **不重新解析**：必须使用 replay 中固定的版本与实现哈希，任何不符都是 422。
- 解析失败或存在歧义时拒绝，不猜、不降级。

## 5. 执行职责

executor（worker）与插件之间是明确的单向边界：

| 由 executor 拥有 | 由插件拥有 |
| --- | --- |
| run 目录、快照/VTK/预览、manifest 与事件 | 参数校验（纯函数） |
| 随机种子、`cpuThreads`、引擎选择与固定 | 在给定 domain 上执行模型 |
| 资源上限检查、超时、RSS 监控 | 返回实际参数、警告、输出材料/表面信息 |
| replay 记录、产物归档、错误分类 | 只返回产物名与元数据，不写盘 |

插件**不得**：写 run 目录以外的路径、创建快照、读取项目文件、选择 flux engine、
修改全局随机状态、根据环境变量改变数值行为。插件只能读 executor 传入的只读
`ProcessContext`；`ProcessResult.outputs` 只是产物名，真正落盘由 executor 完成。
需要随机性时由 executor 传入 `numpy.random.Generator`（或等价的显式种子），插件
不得自行 `seed()` 全局 RNG。

现有实现继续由 executor 调用：`worker/simulate.py` 的 `save_snapshot()`（2600）与
`configure_rays()`（600），以及 `worker/engine_policy.py` 的
`requested_engine_for_step()`（18）/ `verify_worker_engine_evidence()`（42）。插件化
不接管这些函数。

## 6. Replay 与请求哈希兼容

新增的步骤字段是**可选**的 `variant`（对象，缺省 `None`）：

- 旧项目不写 `variant` 时，`model_dump(exclude_none=True)` 不产生该键；
  `backend/hashing.py:56` 的 `requested_input()` 与 `requested_input_hash()`（151）
  得到的字节与今天完全一致。这与 `isotropicMethod` 的既有做法相同。
- 必须有一条回归测试：对同一份不含 `variant` 的 fixture，改动前后请求哈希相等。
- 显式写 `variant` 是真实请求差异，参与请求哈希。

replay 固定以下六项，缺一不可：

1. `pluginId`
2. `pluginVersion`
3. `variantId`
4. `variantVersion`
5. `implementationHash`
6. `engineVersion`（ViennaPS + ViennaLS）

落点：`ReplayRequirements`（`backend/models.py:2272`）增加逐步骤的可选插件身份；
旧 replay 没有这些键时按“legacy native 映射”解析，行为不变。replay 预检发现插件缺失、
版本不符或哈希不符时返回 422，不执行任何步骤，也不留下 run 目录（沿用
`backend/planning.py:94` 的“先校验再建目录”约束）。

## 7. 一致性测试（每个变体强制）

`process_models/conformance.py` 是唯一入口；没有通过记录的变体不能注册为可用。

| 检查 | 证明什么 |
| --- | --- |
| 2D / 3D canonical fixture | 变体在两种维度下都能构造并完成一步 |
| deterministic repeat | 同种子重复运行，输出逐字节一致（或按声明容差一致） |
| snapshot / replay | 快照可重放，replay 结果与首次运行一致 |
| 输出表面 / 材料 ID | 表面存在、材料映射与声明的输出语义一致 |
| 资源边界 | 墙钟、RSS、网格规模不超过 `resourceBounds` |
| UI descriptor | 字段、默认值、单位、枚举与 schema 一致，前端可直接渲染 |
| 参考流程 + golden | 在一条真实参考 flow 中端到端跑通，并与 golden 输出比较 |

**覆盖的定义是最后一行**：只有“参考流程端到端 + golden 输出”才算覆盖。schema 存在、
UI 能显示、单元测试构造成功，都不算覆盖。注册表应把 `conformance` 记录作为可用性的
前置条件，而不是展示用的徽章。

## 8. 信任模型

阶段 1 的假设是：**插件由运维安装并经过审查**，不是任意用户代码。

- 插件运行在 worker 的 bounded child 中（`scripts/bounded_child.py` 已有超时、
  进程组、RSS 监督与工作台目录约束）。
- 插件目录只读；进程只允许写本次 run 目录。
- 禁止网络访问；不向插件进程注入网络凭据。
- 随机性只来自 executor 传入的种子；插件不得读取时间、`os.urandom` 或全局 RNG。
- 确定性设置由 executor 固定：`cpuThreads`、引擎、时间步策略等；插件不得覆盖。
- 阶段 1 不声称已完成内核级 sandbox（netns/seccomp/seccomp-bpf）。如果未来需要运行
  **不可信**插件，再引入独立 plugin host；在那之前，进程边界 + 只读目录 + 哈希固定
  + 人工审查就是全部保证。

## 9. 目标目录布局

```
process_models/
  __init__.py          # 已实现：公开 contract/registry 符号
  contract.py          # 已实现：ProcessPlugin / EngineVariant / ProcessContext /
                       # ProcessState / ProcessResult / ModelDescriptor /
                       # VariantCapabilities / Issue
  registry.py          # 已实现：显式注册、版本+实现哈希解析
  voxel.py             # 已实现：height-field level-set <-> voxel 适配器
  native/
    __init__.py
    _legacy.py         # 待做：包装现有 CATALOG + build_specialized_model，行为不变
    isotropic.py       # 待做：PR2 提取
    sf6o2.py           # 待做：PR2 提取
    oxidation.py       # 待做：PR2 提取
  surrogate/
    __init__.py        # 已实现
    voxel_probe.py     # 已实现：实验性 proof plugin，不是生产模型
    cmp.py             # 待做：PR4 第一个生产 Python surrogate（候选）
  external/
    __init__.py        # 阶段 1 为空；不实现任何外部引擎适配器
  conformance.py       # 待做：fixture 运行、golden 比较、确定性/资源检查
  fixtures/            # 待做：2D/3D canonical 输入与 golden 输出
```

约束：

- `contract.py` 不 import `backend` 或 `worker`，避免循环依赖；引擎版本由调用方传入。
- `registry.py` 是唯一把稳定 ID 解析成实现的模块；worker 不按字符串 `getattr` 找类。
- `native/_legacy.py` 在迁移期必须调用现有 `build_specialized_model()`，不复制原生调用。
- `external/` 只占位；阶段 1 不声称支持任何外部引擎。
- PR1 只落 `contract.py`、`registry.py`、`__init__.py` 与 legacy 包装；`native/` 下其余
  模块由后续 PR 按 §11 逐步加入。

## 10. 工艺分类（初稿）

按“家族 → 叶单元工艺”分组。现有 `model` / `nativeModel` 是迁移映射，不是最终 ID。

| 家族 | 叶单元工艺 | 现有 ID | 迁移安排 |
| --- | --- | --- | --- |
| 各向同性 | 各向同性刻蚀、各向同性沉积 | `isotropic`（levelSet / geometric 两条原生路径） | 提取为插件；两条路径先作为同一插件的两个 native 变体 |
| 定向 | 定向刻蚀/沉积 | `directional` | 提取 |
| 等离子体刻蚀 | SF₆/O₂、CF₄/O₂、HBr/O₂、SF₆/C₄F₈、fluorocarbon、IBE、Faraday cage | `sf6o2`、`cf4o2`、`hbro2`、`sf6c4f8`、`fluorocarbon`、`ionBeam`、`faradayCage` | 按化学逐个提取；`sf6o2` 先做 |
| 湿法刻蚀 | 晶向湿法刻蚀 | `wetEtch` | 后续提取 |
| 热氧化 | 干/湿氧化、氧化层再生 | `oxidation`、`oxideRegrowth` | `oxidation` 先做；`oxideRegrowth` 后续 |
| 沉积 | ALD、TEOS 热沉积、TEOS PECVD、选择性外延、几何沟槽沉积 | `ald`、`teosDeposition`、`teospecvd`、`selectiveEpitaxy`、`geometricTrenchDeposition` | 后续提取 |
| 平坦化（CMP） | 材质感知平坦化 | `planarize` | PR4 的 surrogate 候选；native 变体保留 |
| 注入 | 离子注入 | `implant` | 后续提取 |
| 退火 | 退火/扩散 | `anneal` | 后续提取 |
| 分析 | 厚度/剖面/通量分析 | `analysis` | 后续；可能不按“物理单元工艺”建模 |
| 输运辅助 | 中性粒子输运/覆盖率 | `neutralTransport` | 归入等离子体家族或独立辅助插件，待定 |
| 几何/布尔 | 去材料、布尔、GDS 布尔、粒子形状、CSV 过程 | `removeMaterial`、`boolean`、`gdsBoolean`、`particleShape`、`csvProcess` | **不**算单元工艺；阶段 1 留在 executor 内 |

> 最终粒度仍开放：`sf6o2` 与 `cf4o2` 是“一个等离子体刻蚀插件 + 化学参数”，还是
> 两个独立插件？各向同性的 levelSet/geometric 是同一插件的两个变体，还是两个插件？
> 这些在 PR2 前必须定稿（见 §13）。分类表的作用是划定范围，不是提前锁死 ID。

## 11. 迁移顺序

PR1 必须保持 `AGENTS.md` 的基线：**108 项（103 通过 / 0 失败 / 5 跳过）**，快照
字段/顺序、事件、原生调用顺序、已有哈希都不变。后续 PR 只追加用例，不改写既有断言。

> 当前分支完成了 PR1 的 **契约 + 注册表 + voxel 适配器 + proof plugin**；
> `native/_legacy.py`、worker 接入、真实工艺插件、`variant` 字段和 API/UI 仍未做。
> 定向测试 38 项全部通过；没有改动既有 schema、哈希、快照或原生调用。

| PR | 内容 | 行为约束 |
| --- | --- | --- |
| 1 | `contract.py` + `registry.py` + `native/_legacy.py` | 纯包装：现有 `CATALOG` 与 `build_specialized_model()` 通过 legacy 变体暴露；schema、UI、校验、worker 调用全部走原路径；哈希与产物逐字节不变 |
| 2 | 提取 `isotropic` / `sf6o2` / `oxidation` 到 `native/` | 逐模型搬移，native 调用与参数映射不变；每步都有对照测试；不回退到 legacy |
| 3 | 可选 `variant` 字段 + 通用 API/UI | 缺省不进入请求哈希；显式写入才参与；前端只读 registry 描述符 |
| 4 | 第一个 Python surrogate（CMP 候选） | 通过完整一致性测试；默认仍是 native；不自动选择 surrogate |
| 5 | 提取其余模型 | 按家族分批，每批独立可回滚 |

明确不做 big-bang 重写 `worker/simulate.py`：它承担快照、种子、引擎策略与产物，
属于 executor 的稳定面。插件化只改“模型构造与执行”这一段。

## 12. 阶段 1 非目标

- 不做 voxel-native flow：voxel 只作为单步适配器，flow state 仍是 level-set。
- 不允许任意用户插件上传或运行时安装；只有运维安装、哈希固定的插件。
- 不做 UI 插件市场、在线安装、版本浏览或权限管理。
- 不实现 external engine 适配器；`external/` 只占位。
- 不做插件自动选择/自动降级；变体必须显式指定或使用插件默认值。
- 不承诺 Python surrogate 的物理精度；只承诺一致性测试覆盖的范围。
- 不声称已完成内核级 sandbox 或不可信插件隔离。

## 13. 开放决策

| 问题 | 现状 | 需要决定 |
| --- | --- | --- |
| 插件粒度 | §10 是初稿 | 按化学拆（`sf6o2` 独立）还是按家族拆（`plasma-etch` + 化学参数）；几何/分析是否进入插件边界 |
| 变体选择 | 默认 native、逐步骤显式 | 是否允许“按维度/几何自动选变体”；若允许，选择原因必须写入 `actualParameters`，且不得静默降级 |
| 确定性容差 | 一致性测试要求 repeat | `strict` 与 `tolerance` 的判定口径；voxel 往返与 surrogate 的容差如何按 `gridDelta` 定义 |
| 插件版本保留 | 请求可省略版本并解析到最高语义版本；replay 必须固定版本 | 旧版本保留多久、何时允许删除；删除后旧 replay 是 422 还是提供只读归档 |
| `nativeModel` 的去留 | 迁移期映射到 `pluginId` | 新字段稳定后是否弃用、何时弃用、旧项目如何继续解析 |

## 14. 不声称

- 插件运行时只到第一切片的**契约 + 注册表 + voxel 适配器 + proof plugin**；
  还没有 legacy 包装、worker 接入、真实工艺插件、API/UI 或自动变体选择。
- 没有生产级 Python surrogate；`voxel_probe` 只是适配器 proof，CMP 仍是候选。
- voxel 转换只对 height-field 几何有证据；fin/GDS 被拒绝，没有 voxel 物理模型，
  也没有跨几何的精度数据。
- 没有外部引擎适配器，也没有不可信插件隔离。
- 没有改变任何现有 schema、哈希、快照、事件或原生调用；这些要等对应 PR 并附证据。

## 15. 第一切片实现状态（2026-11-01）

| 项 | 状态 |
| --- | --- |
| `process_models/contract.py` | 已实现：`ProcessPlugin` / `EngineVariant` / `ProcessContext` / `ProcessState` / `ProcessResult` / `ModelDescriptor` / `VariantCapabilities` / `Issue` |
| `process_models/registry.py` | 已实现：显式注册、多版本、严格 pin、源码实现哈希 |
| `process_models/voxel.py` | 已实现：`VoxelGrid`、`read_voxels`、`write_voxels`、`round_trip` |
| `process_models/surrogate/voxel_probe.py` | 已实现：identity / erode_top_layer 的 proof variant |
| 依赖 | `numpy==2.5.4` 已加入 `requirements.in`、`requirements.lock`、`requirements-v470.lock` |
| 定向测试 | 38 项通过（contract 14、voxel adapter 15、voxel plugin 9） |
| 支持几何 | 2D/3D plane、stack、trench、hole（wafer-covering mask）、thin film |
| 不支持 | fin/GDS cut-out（读取时拒绝）、新增材料、删除中间层、scalar fields、worker 接入 |
| 往返精度 | plane/thin film 为 0；trench 约 1 个 `gridDelta` 的阶梯误差（probe 实测 0.0998–0.1054 µm @ `gridDelta=0.1`） |
| 原生路径 | 读取 `ToVoxelMesh`；回写边界网格 + `FromSurfaceMesh`；不使用 `FromVolumeMesh` |

下一步（按 §11）：`native/_legacy.py` 包装现有模型 → 提取 `isotropic` / `sf6o2` /
`oxidation` → 可选 `variant` 字段 + `/api/process-models` → 第一个生产 Python
surrogate（CMP）。
