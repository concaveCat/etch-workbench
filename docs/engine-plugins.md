# Engine plugins：ViennaPS 与自定义引擎同一条入口

## 目标

平台 core 只认识这些契约：

- `EngineRequirement` / `EngineCapabilities`
- `EngineProvider` / `EngineLease`
- `StateBackend` / `SurfaceBackend`
- `ProcessSpec` / `VariantRegistration`

ViennaPS 不再由 worker 直接 `new`，而是和自定义引擎一样通过
`engine_plugins/*/engine.toml` + `engines.lock` 加载、校验、组合。

## 安装单元

```text
engine_plugins/
  viennaps_engine/
    engine.toml
    plugin.py
    ...
  my_engine/
    engine.toml
    plugin.py
    ...
```

`engine.toml` 只允许：

```toml
api = 1
engine_id = "myengine"
version = "1.0.0"
entrypoint = "my_engine.plugin:PLUGIN"
description = "..."
```

`entrypoint` 指向一个 `EnginePlugin` 对象：

```python
class EnginePlugin(Protocol):
    engine_id: str
    version: str
    def create_provider(self, context: EngineContext) -> EngineProvider: ...
```

`EngineContext` 只提供 run 级通用输入：

- `project`
- `run_dir`
- `selected_device`
- `config`
- `fingerprint`

引擎插件自己 import 自己的 native package；平台不把 `ps` 或 ViennaPS
`Domain` 传进 core。

## Lock

```bash
.venv/bin/python scripts/lock-engines.py
.venv/bin/python scripts/lock-engines.py --check
```

`engines.lock` 固定：

- `engine.toml` 字节哈希
- engine package `*.py` 实现哈希
- `engine_id` / `version` / `entrypoint`

任何漂移都会在加载时失败，不会回退到另一个 provider。

## Worker 组合

`worker/process_models_v2.py::build_worker_v2_session()` 现在：

1. `load_engine_registry()`
2. `engine_registry.provider(EngineContext(...))`
3. `FlowSession(process_registry, provider, store_factory)`

ViennaPS 和自定义引擎都走同一行。`WorkerV2Session` 每个 run 只构建一次
provider；每步仍然单独执行、单独 snapshot、单独写 replay artifact。

## ProjectV7 process step

自定义 process plugin 通过 envelope 提交：

```json
{
  "id": "plugin_step_01",
  "name": "my process",
  "enabled": true,
  "model": "plugin",
  "pluginId": "my_process",
  "params": {
    "operation": "etch",
    "rateMagnitude": 0.1,
    "duration": 1.0
  }
}
```

v2 dispatcher 会展开为 `model=pluginId` 的原始 step，再调用插件的
`parse_step`。`model="plugin"` 无论 v2 默认开关如何都会走 registry 路径。

## solver 选择

```json
"solver": {
  "engineId": "myengine",
  "engineKernel": "MY_KERNEL",
  "engineBackend": "cpu"
}
```

`engineId` / `engineKernel` 必须同时出现，且不能与原生 `gpuEngine` 同时使用。
未安装的 `engineId` 会在项目校验阶段被拒绝。

## 自定义引擎需要实现什么

- `EngineProvider.engines()` / `acquire()`
- `EngineLease` 的 `handle` / `profile` / `state_backend` / `fingerprint` / `release`
- `StateBackend`：`checkpoint` / `write` / `load` / `validate`
- 可选 `SurfaceBackend`：`extract(state) -> SurfaceData`
- 可选 `CellSetBackend`

如果引擎不提供 `SurfaceBackend`，worker 只能使用该引擎自己的 native snapshot
路径；提供后，平台可以用 `worker/v2_snapshot.py` 生成 viewer 需要的通用
points/cells/materialIds 快照。

## 当前状态

| 能力 | 状态 |
|---|---|
| ViennaPS 通过 engine registry 加载 | ✅ |
| 自定义 engine plugin 通过同一 registry 加载 | ✅ |
| 自定义 engine provider 注入 worker | ✅ |
| 自定义 kernel 不再被 native 白名单拒绝 | ✅ |
| 自定义 engine 的 `SurfaceBackend` 通用快照 | ✅ 新增 |
| `/api/engines` 列出已安装 engine | ✅ |
| ProjectV7 `model="plugin"` process step | ✅ 新增 |
| `solver.engineId` / `engineKernel` / `engineBackend` | ✅ 新增 |
| Legacy UI engine picker | ✅ 新增 |
| ViennaPS rich snapshot / 测量 | ✅ 保留原路径 |
| geometry / cell-set 完全 capability 化 | 仍使用 ViennaPS helper；下一步迁移 |
| 原生 step 的 v2 默认路径 | 仍为 opt-in；`model="plugin"` 强制走 v2 |

## 设计边界

- core 不 import `engine_plugins.viennaps_engine`
- engine plugin 不 import `worker` / `backend`
- process plugin 不构造 provider；只声明 `EngineRequirement`
- 没有匹配 engine 时 `EngineUnavailableError`，不 fallback
- 每步 replay 仍由 `FlowExecutor` 写 `state/outputs/plan/geometry`
