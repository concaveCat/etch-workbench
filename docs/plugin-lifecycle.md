# 插件生命周期：manifest、发现与锁

> 范围：`process_models/plugins/`、`plugins/`、`plugins.lock`、`scripts/lock-plugins.py`。
> 本文描述当前 v2.4 插件生命周期：把一个插件从“代码里的一个类”变成
> **可安装、可校验、可 pin 的单元**。manifest、发现、lock、loader 均已落地；
> 是否可用以仓库文件、以及 `scripts/lock-plugins.py --check` 的退出码为准。
>
> 精确的定向测试结果见 [`process-model-plugins-v2.md`](process-model-plugins-v2.md) §9.2；
> 本文不重复数字。
>
> 契约、分层与执行语义见 [`process-model-plugins-v2.md`](process-model-plugins-v2.md)；
> 本文只讲“插件作为安装单元”这一层。

## 1. 插件是什么

一个插件 = **manifest + entrypoint + 过程规格/变体**，三者缺一不可：

| 组成 | 载体 | 回答的问题 |
| --- | --- | --- |
| manifest | `plugins/<id>/plugin.toml` | 我是谁、哪个版本、用哪一版插件 API、入口在哪 |
| entrypoint | `module:attr` 指向的注册函数 | 怎么把实现装进 `ProcessRegistry` |
| 过程规格 / 变体 | `ProcessSpec` + `VariantRegistration`（v2 契约对象） | 这一步的语义、参数、能力、引擎、实现哈希 |

关键不变量：

- **项目 JSON 永远不出现模块路径或 callable。** JSON 里只有 `process_id`、
  `variant_id` 和参数；选择“跑哪份代码”的是运维控制的 manifest + lock，不是用户输入。
- 插件作者仍然写 Python（`ProcessSpec` + `parse_step` + `EngineVariant` + `register`），
  manifest 只是让这份代码**可安装、可校验**，不改变 v2 的注册方式。
- 发现与加载只读 manifest。loader 不会从项目 JSON、请求体或任意用户字段里
  import 模块。

## 2. 目录布局与发现

```
process_models/plugins/     # 生命周期库：manifest / lock / loader / errors
plugins/
  isotropic/
    plugin.toml             # 树内唯一注册插件的 manifest
    params.py
    step.py                 # parse_step(project, step)
    spec.py
    register.py
    variants/native.py
plugins.lock                # 由脚本生成，随部署一起发布
scripts/lock-plugins.py     # 生成 / 校验
```

发现规则（`process_models.plugins.loader`）：

1. 目录列表来自环境变量 `VIENNAPS_WORKBENCH_PLUGIN_DIRS`，用 `os.pathsep`
   （Linux 上是 `:`）分隔；相对路径按仓库根解析。
2. 未设置时，唯一默认目录是 `<repo>/plugins`。
3. 每个插件目录**直接**包含 `plugin.toml`，即扫描 `dir/*/plugin.toml`，不递归。
4. 导入 entrypoint 之前，把**插件目录的父目录**（以及仓库根）加入 `sys.path`，
   这样运维可以把包目录和 manifest 放在一起；树内 entrypoint（`process_models.*`）
   照常可导入。

| 环境变量 | 作用 | 缺省 |
| --- | --- | --- |
| `VIENNAPS_WORKBENCH_PLUGIN_DIRS` | 发现根，多个用 `os.pathsep` 分隔 | `<repo>/plugins` |
| `VIENNAPS_WORKBENCH_PLUGIN_LOCK` | lock 文件路径 | `<repo>/plugins.lock` |

**信任模型是“运维安装”，不是“用户上传”。** 插件目录属于运维控制面：

- 目录内容等价于在同一 `.venv` 里执行的代码，权限与 worker 相同；
- 不要把 `VIENNAPS_WORKBENCH_PLUGIN_DIRS` 指向用户可写、可同步或网络挂载的目录；
- 本切片**没有**沙箱、签名校验或权限降级，见 §7。

## 3. manifest 字段与严格性

```toml
api = 1
id = "isotropic"
version = "1.0.0"
entrypoint = "plugins.isotropic:register"
description = "Native isotropic etch/deposit"
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `api` | int | 必须等于 `PLUGIN_API_VERSION`（当前 `1`）；不等即拒绝加载 |
| `id` | str | 非空；同时是 `ProcessSpec.id` |
| `version` | str | 非空；同时是 `ProcessSpec.version` |
| `entrypoint` | str | 恰好一个 `module:attribute`，无空格、无链式属性（`a.b:r.f` 无效） |
| `description` | str | 可选，缺省空串；可读说明，不参与准入 |

严格性：

- **未知键直接报错**，不忽略、不警告后继续——拼错的键（如 `entrypont`）不会
  变成“默认值”悄悄生效。缺失必需键同理。
- 解析结果是 frozen dataclass（`read_manifest(path) -> PluginManifest`），
  manifest 在加载后不可变。
- TOML 语法错误、缺字段、类型不符、`api` 不符，都在读取阶段失败，早于 import。

**API 版本语义**：`api = 1` 同时约定 manifest 字段集、`plugins.lock` 的
`schemaVersion`/`apiVersion = 1`、以及 `register(registry)` 的调用形状。任何不兼容
改动都要升 `PLUGIN_API_VERSION` 并同时升 lock；不存在“新旧混读”的兼容分支。

## 4. `plugins.lock`

lock 是**运维签字**：它把“这次部署实际装的是哪份代码”写成可 diff 的 JSON，
而不是运行时临时扫描的结果。

```json
{
  "schemaVersion": 1,
  "apiVersion": 1,
  "plugins": [
    {
      "id": "isotropic",
      "version": "1.0.0",
      "api": 1,
      "entrypoint": "plugins.isotropic:register",
      "manifestSha256": "<plugin.toml 原始字节的 sha256>",
      "variants": {
        "native": {
          "version": "1.0.0",
          "implementationHash": "<registry.compute_implementation_hash 的结果>"
        }
      }
    }
  ]
}
```

格式约定：`plugins` 按 `(id, version)` 排序，`variants` 按 id 排序，`indent=2`，
结尾一个换行——保证同一输入生成字节级相同的文件，lock 改动在 diff 里可见。

### 4.1 生成与校验

```bash
# 生成/重写锁，并打印写入路径
.venv/bin/python scripts/lock-plugins.py

# 只校验：0 = 一致，1 = 漂移/缺失/损坏
.venv/bin/python scripts/lock-plugins.py --check
```

等价 API：`process_models.plugins` 公开 `write_lock(...)`、`verify_lock(...)`、
`load_registry(...)`；底层读写是 `read_lock`/`write_lock_file`。脚本不启动服务，
只做 manifest 发现、建临时 registry、算哈希。

### 4.2 哈希到底覆盖什么

| 字段 | 覆盖范围 | 能发现什么 |
| --- | --- | --- |
| `manifestSha256` | `plugin.toml` 的**原始字节**（不是解析后的值） | 改注释、改顺序、改空白也会变；身份字段被改必然变 |
| `variants.<id>.implementationHash` | `compute_implementation_hash()`：如果变体在 `plugins/<id>/` 包内，覆盖该包**全部 `.py` 字节**（含 `step.py`/`params.py`/`spec.py`/`register.py`），再加变体声明的 `implementation_files`（isotropic 用它覆盖 `process_models/v2/engines/viennaps.py`），最后混入 `模块.qualname` | 版本号没升但实现改了；包内任一模块或共享引擎 helper 变化 |

lock **不保存代码副本**，只保存哈希。因此字节级复现依赖“同一份代码 + 同一份
lock”。第三方行为（`viennaps`/`numpy` 版本）由 `requirements*.lock` 与
`engine_version`/`engine_fingerprint` 负责，不由插件哈希负责（同 v2 文档 §5）。

### 4.3 失败即关闭（fail closed）

`load_registry()` 的顺序是固定的：读 lock → 发现 manifest → 比对条目集合（多一条或
少一条都报错）→ import entrypoint 并注册 → 比对 api/entrypoint/manifest 字节/变体集合
与版本/实现哈希。任一步不符都抛 `PluginLifecycleError`，**没有回退、没有“警告后
继续”、没有“用最新版本顶替”**。lock 自身也严格：未知键、错误的
`schemaVersion`/`apiVersion`、不是小写 64 位 hex 的哈希、重复的 `(id, version)`
都在读取阶段报错。

`write_lock()` 走同一套发现与注册流程，并额外建一个合并 registry；**写不出来就说明
锁加载不了**，所以生成阶段也会拒绝重复身份与冲突实现。

| 情况 | 结果 |
| --- | --- |
| lock 文件缺失 | 报错（不按“空 lock”继续） |
| manifest 缺失 / 多出 / 重复 `(id, version)` | 报错 |
| lock 多一条 / 少一条插件条目 | 报错 |
| `api`、`id`、`version`、`entrypoint` 与 lock 不符 | 报错 |
| manifest 字节被改（`manifestSha256` 不符） | 报错 |
| 变体 id/版本与 lock 不符 | 报错 |
| 实现字节被改（`implementationHash` 不符） | 报错 |
| entrypoint 模块/属性不存在，或 import 失败 | 报错 |
| 注册出的 process/variant 超出 manifest 的 id/version | 报错 |

**不要手改 lock。** 改插件就重新生成 lock；手改会让下一步校验失败，而且 lock 的
价值正是“只有脚本能写”。

## 5. 多版本与 replay

- 同一个 `id` 允许**多个版本共存**：每个版本一个 manifest、一条 lock 条目；
  `(id, version)` 是唯一的，重复即报错。
- lock 固定的是**当前已安装的版本集合**，不是全部历史版本。它不提供归档、
  下载或自动回滚。
- replay 需要某个**未安装**的版本时，是**硬错误**：既不降级到邻近版本，也不改用
  “最新版本”。要 replay 旧 run，就必须同时保留旧代码目录与它的 lock 条目。
- 与执行身份的关系：run 记录保存 `ExecutionPlan.identity_json()` 的九个 camelCase 键
  （`processId`、`processVersion`、`variantId`、`variantVersion`、`implementationHash`、
  `engineId`、`engineVersion`、`engineFingerprint`、`adapter`；见 v2 文档 §5）。
  `manifestSha256` 属于**安装面证据**，不写进 plan identity；需要字节级复现时，归档
  “代码 + lock”这一对。
- 可执行的 replay pin 目前只有 `expected_process_version` / `expected_variant_version`；
  `implementationHash` / engine fingerprint 只被记录，不能重新 pin。
- 本切片不做版本保留策略（留多久、删了之后旧 replay 怎么办），该问题仍挂在
  v2 文档 §10.2。

## 6. 新增 / 升级 / 移除插件

三种操作都不改变契约：**项目 schema、项目 JSON、resolver 一律不动**。

**新增**

1. 建 `plugins/<id>/plugin.toml`（`api`/`id`/`version`/`entrypoint` 必填，
   `description` 可选）。
2. 按 `docs/plugin-template/` 的布局实现 `params.py`、`step.py`（`parse_step`）、
   `spec.py`（`default_variant` + `parse_step`）、`variants/` 和
   `register(registry)`；注册内部仍是 v2 的 `register_process(spec)` +
   `register_variant(VariantRegistration(...))`。
3. 运行 `scripts/lock-plugins.py` 生成/更新 `plugins.lock`。
4. 运行 `scripts/lock-plugins.py --check`，必须退出 0。
5. **重启 worker / 服务**：发现与 registry 在进程启动时构建，没有热加载。

**升级**

1. 用新目录/新版本号并存，不要原地改一个已发布版本的文件。
2. 升 `plugin.toml` 的 `version`（语义/参数变还要升 `ProcessSpec.version`；
   实现变要升变体 `version`）。
3. 重新生成 lock；旧条目按保留策略决定去留。
4. 重启服务。原地改代码但不动版本号时，`--check` 会以
   `implementationHash` 不符报错——这是有意的。

**移除**

1. 删插件目录，然后重新生成 lock（只删目录不重生成会因“manifest 缺失”报错，
   这正是防止插件悄悄消失）。
2. 重启服务。
3. 确认没有待 replay 的 run 依赖被移除的版本。

**发布面**：`plugins.lock` 必须与 `plugins/` 在同一份部署产物里一起发布，版本必须
对得上；镜像里只带其中一个，等于把 lock 变成谎言。

## 7. 明确不包含

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 跨插件一致性套件 | 不包含 | `process_models/v2/conformance.py` 提供单变体 `check_variant()`；没有跨插件/跨目录的批量 conformance 套件 |
| 运行时 warm pool / 租约池 | 不包含 | registry 每次构建，import 由 Python 缓存；`StaticEngine.release()` 仍是 no-op |
| HTTP API / UI | 不包含 | 没有“已安装插件”端点、没有上传页 |
| 插件市场 / 分发 / 签名 | 不包含 | 安装 = 运维把目录放进发现根并生成 lock |
| 不可信代码沙箱 | 不包含 | 无 netns/seccomp/独立进程或用户；插件与 worker 同权限、同 `.venv` |
| 自动引擎/变体选择 | 不包含 | 仍是显式 `variant_id`，无降级 |
| 热加载 / 运行时安装 | 不包含 | 改插件必须重启进程 |
| per-plugin 依赖隔离 | 不包含 | 所有插件共用仓库 `.venv` |
| 版本归档 / 自动回滚 | 不包含 | 多版本共存靠运维同时安装 |

## 8. Checklist

### 运维

- [ ] 发现根只包含运维控制的路径；没有指向用户可写、共享或网络目录。
- [ ] `.venv/bin/python scripts/lock-plugins.py --check` 退出 0。
- [ ] `plugins.lock` 与 `plugins/` 来自同一次发布。
- [ ] 新增/升级/移除后已重启服务（无热加载）。
- [ ] 需要 replay 的旧版本：代码目录与对应 lock 条目都保留。
- [ ] 归档一份旧 lock，便于对照“当时装的是什么”。

### 开发

- [ ] manifest 只写 `api`/`id`/`version`/`entrypoint`/`description`，不加自定义键。
- [ ] manifest 的 `id`/`version` 与 `ProcessSpec` 一致；`api` 跟随
      `PLUGIN_API_VERSION`。
- [ ] entrypoint 是单个 `module:attr`，指向幂等的 `register`。
- [ ] `step.py` 的 `parse_step(project, step)` 只认领本工艺；不属于时返回 `None`，
      字段缺失时抛 `ValueError`；`spec.py` 里显式写 `default_variant` 和
      `parse_step=parse_step`。
- [ ] 改实现就升变体版本，并重新生成 lock；不手改 `plugins.lock`。
- [ ] 项目 JSON 里没有也不新增 module/callable 字段。
- [ ] 跑 lifecycle 单测、dispatch 单测与 worker bridge 单测
      （`tests/test_plugin_lifecycle.py`、`tests/test_worker_v2_isotropic.py`、
      `tests/test_v2_conformance_native.py`）——结果见 v2 文档 §9.2。
- [ ] 不碰 v1（`process_models/contract.py`、`registry.py`、`voxel.py`）、
      `backend/`、`frontend/`，不新增 process model。

## 9. 当前清单

本切片注册的插件只有一个；`voxel_probe` 仍是树内 proof 插件，**不在** `plugins/`
目录、也**不在** `plugins.lock` 里（只能由代码显式 `register_voxel_probe` 注册）。
当前 `plugins.lock` 只包含下表这一条；查看其完整身份（含两个摘要）：

```bash
.venv/bin/python -m json.tool plugins.lock
```

| 项 | `isotropic` |
| --- | --- |
| 类型 | 原生（native）工艺插件 |
| 树内位置 | `plugins/isotropic/` |
| manifest | `plugins/isotropic/plugin.toml` |
| `api` | `1` |
| `id` @ `version` | `isotropic` @ `1.0.0` |
| `entrypoint` | `plugins.isotropic:register` |
| 变体 | `native` @ `1.0.0`（2D/3D，etch/deposit） |
| 单步 dispatch | `spec.py` 绑定 `step.py::parse_step`；`default_variant="native"` |
| lock 条目 | `plugins` 数组中唯一一条 `("isotropic", "1.0.0")` |
| `manifestSha256` | 由 `scripts/lock-plugins.py` 生成，以仓库内 `plugins.lock` 为准 |
| `variants.native.implementationHash` | 同上（`compute_implementation_hash` 结果） |

文档不抄写这两个十六进制摘要：它们随文件字节变化，抄写即过期；判断依据永远是
`scripts/lock-plugins.py --check` 的退出码。

| 未注册插件 | 位置 | 为什么不在 lock 里 |
| --- | --- | --- |
| `voxel_probe` @ `1.0.0` | `process_models/v2/surrogate/voxel_probe.py` | surrogate **proof** 插件，仅用于验证 level-set ↔ voxel 适配器；没有 manifest，不是安装单元 |

第二个真实工艺（`sf6o2` 等）落地时复制 `docs/plugin-template/`、按 §6 新增目录与 lock
条目即可，不需要改契约。
