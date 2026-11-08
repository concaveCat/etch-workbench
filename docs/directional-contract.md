# DirectionalProcess 项目合同

本合同适用于候选目录中的 schema 5。schema 1–4 的结构与哈希规则保持原样。

## 输入

- `direction` 是三个有限分量，每个分量在 [-1, 1]，且向量非零；执行前单位化。
- 二维输入顺序为 `[x, h, 0]`，其中 `h` 为截面高度；三维顺序为 `[x, y, z]`，`z` 为垂直方向。
- `directionalRateMagnitude` 和 `isotropicRateMagnitude` 是非负速率大小，范围各为 0–2 µm/s。
- 操作映射原生速度符号：刻蚀为负，沉积为正。该符号约定已通过本机平面表面原生调用核实。
- `calculateVisibility` 控制定向项的遮挡可见性。定向速率为零时该值不适用；全零速率步骤不调用 ViennaPS Process，也不改变几何。
- 掩膜由项目几何和层角色推导，并以本机绑定支持的位置参数列表传入。材料名和 ID 都写入实际执行记录。
- 沉积前按所选材料复制顶层 level set；刻蚀不得携带 `depositMaterial`。
- 定向过程目前只开放已验证的 CPU 路径；schema 5 定向步骤请求 GPU 时验证失败，不会静默使用 CPU。

## 多 RateSet（`rateSets`，schema 5+ 附加面）

原生 4.6.2 绑定同时提供 `ps.DirectionalProcess(rateSets: Sequence[RateSet])` 与
`ps.DirectionalProcess(rateSet: RateSet)` 重载（`viennaps.d2` 与 `viennaps.d3` 相同）。
`directional` 步骤因此新增可选 `rateSets`；省略它时请求字段、请求哈希与原生化调用
（单速度重载）完全不变。两者互斥：

- `rateSets` 与 `direction`、`directionalRateMagnitude`、`isotropicRateMagnitude` 不能同时出现；
- 使用 `rateSets` 时步骤级 `calculateVisibility` 必须省略或为 `true`，可见性按集合给出。

```json
{
  "id": "step_dual_beam",
  "name": "双方向束",
  "enabled": true,
  "operation": "etch",
  "model": "directional",
  "rateSets": [
    {
      "direction": [0.0, 1.0, 0.0],
      "directionalVelocity": 0.15,
      "isotropicVelocity": 0.0,
      "maskMaterials": ["Mask"],
      "calculateVisibility": true
    },
    {
      "direction": [0.6, -0.8, 0.0],
      "directionalVelocity": 0.1,
      "isotropicVelocity": 0.02
    }
  ],
  "duration": 0.4
}
```

字段语义（定义见 `backend/models.py:DirectionalRateSetV5` 与 `DirectionalStepV5`）：

| 字段 | 必选 | 约束 | 语义 |
| --- | --- | --- | --- |
| `direction` | 是 | 3 个有限分量，各在 [-1, 1]，非零 | 该集合的入射方向；执行前单位化。2D 必须为 `[x, h, 0]`，3D 为 `[x, y, z]` |
| `directionalVelocity` | 是 | 0–2 µm/s | 非负速率大小；原生符号由 `operation` 决定（刻蚀为负、沉积为正）。名字沿用原生属性，但值不是带符号的原始速度 |
| `isotropicVelocity` | 否（默认 0） | 0–2 µm/s | 非负各向同性速率大小；符号规则同上 |
| `maskMaterials` | 否 | 0–16 个不重复材料名，必须来自 `/api/materials`（内置或已注册自定义材料） | 省略时继承项目几何推导的掩膜（与标量路径相同）；显式列表（含 `[]`）原样使用 |
| `calculateVisibility` | 否 | 布尔，默认 `true` | 仅对该集合生效；`directionalVelocity = 0` 时原生强制关闭并记录为 not-applicable |
| `rateSets` | 否 | 1–8 项 | 每个元素对应一个真实 `ps.RateSet` |

约束与拒绝条件：

- `rateSets` 长度为 1–8；空列表被拒绝（原生虽接受空列表，但那样不会构建任何速度场）。
- 每个 `direction` 非零；2D 第三分量必须为 0（按单位化后的方向逐集合/标量检查：归一化后 `|z| > 1e-12` 即拒绝，不再只看原始分量的绝对值）。
- 每个材料名必须在材料目录中；重复名称、未知名称、超出范围的速率都会被拒绝。
- 不接受自定义 callable 或任何额外字段（pydantic `extra="forbid"`）。
- `maskMaterials` 显式省略了几何掩膜材料时，`/api/projects/validate` 返回警告：该集合会移动这些材料。掩膜从不被静默丢弃，默认值与逐集合记录都会写入运行历史。

### API 消费方（rateSets 模式必须分支）

`rateSets` 模式下步骤的标量字段 `direction`、`directionalRateMagnitude`、
`isotropicRateMagnitude` **不存在**（`model_dump(exclude_none=True)` 会省略 `null`，
`/api/projects/validate` 返回的 `normalizedProject` 与保存后的项目同样如此）。
它们不是 0，也不应被补成 0：

- 消费方必须先判断 `step.get("rateSets") is not None`，再决定渲染/回写哪一组字段；
- rateSets 分支只能回写 `rateSets`（以及与模式无关的 `id`、`name`、`enabled`、
  `operation`、`model`、`duration`、`depositMaterial`），不得写入标量字段；
- 步骤级 `calculateVisibility` 在 `rateSets` 步骤的标准化输出中仍以默认值 `true` 出现，
  回写 `true` 是允许的（与默认等价），但不得回写 `false`（会 422）；可见性按集合放在
  `rateSets[]` 内。

最小正确请求（回写时保持这个形状）：

```json
{
  "id": "step_dual_beam",
  "name": "双方向束",
  "enabled": true,
  "operation": "etch",
  "model": "directional",
  "rateSets": [
    {"direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.15},
    {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.1}
  ],
  "duration": 0.4
}
```

错误示例：旧消费方把缺失的标量字段补成 0 后回写。

```json
{
  "id": "step_dual_beam",
  "name": "双方向束",
  "enabled": true,
  "operation": "etch",
  "model": "directional",
  "rateSets": [{"direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.15}],
  "direction": [0.0, 0.0, 0.0],
  "directionalRateMagnitude": 0.0,
  "isotropicRateMagnitude": 0.0,
  "duration": 0.4
}
```

`POST /api/runs`、`POST /api/runs/replay`、`POST /api/projects` 与
`POST /api/projects/validate` 都会返回 422。路径前缀取决于端点：`/api/projects` 与
`/api/projects/validate` 直接校验 JSON，路径为 `steps[0].directional`；`/api/runs` 的
请求体先经 `ProjectAny` 判别联合，实际是 `project[5].steps[0].directional`；
`/api/runs/replay` 的整个信封也是判别联合，实际是
`[5].normalizedProject.steps[0].directional`。`message` 相同：

```json
{
  "detail": [
    {
      "path": "project[5].steps[0].directional",
      "message": "Value error, rateSets is mutually exclusive with direction, directionalRateMagnitude and isotropicRateMagnitude: omit all three fields in rateSets mode; send them only for scalar directional steps, never as 0 placeholders"
    }
  ]
}
```

该互斥是刻意保留的服务端约束：服务端不会静默丢弃回写的 0，也不会猜测消费方想切换到标量模式。
把三个字段同时删除（而不是置 0）即可通过；若确实要标量模式，删除 `rateSets` 并只发送标量字段。

## ViennaPS 调用

运行使用安装的 ViennaPS 4.6.2 / ViennaLS 5.8.5，经 `scripts/bounded_child.py` 派生并受时限、内存、日志和回收策略约束。标量模式执行调用形态为：

```python
ps.DirectionalProcess(unit_direction, signed_directional_rate,
                      signed_isotropic_rate, mask_materials, calculate_visibility)
```

`rateSets` 模式构造真实 RateSet 对象并调用序列重载：

```python
ps.DirectionalProcess([
    ps.RateSet(unit_direction_i, signed_directional_velocity_i,
               signed_isotropic_velocity_i, mask_materials_i, calculate_visibility_i),
    ...
])
```

本机绑定接受第四个位置参数 `maskMaterial` 的材料列表；`maskMaterials=` 关键字不受该版本绑定接受。该模型不执行射线通量采样，不配置 `RayTracingParameters`，随机种子和 `raysPerPoint` 记录为不适用。由于当前 `Process` 构造默认会探测 CUDA，工作进程显式钉住 `CPU_DISK` 后执行；这不是一个随机射线引擎。

原生多集合语义（4.6.2 源码与 `worker/probe_ratesets_native.py` 实测）：

- 定向速度逐集合相加：每个未掩膜、可见性通过且 `direction·normal != 0` 的集合贡献
  `-direction*directionalVelocity`；方向相反的集合会相互抵消。
- 各向同性速度逐集合相加，只对未掩膜集合计入。
- `maskMaterials` 与 `calculateVisibility` 都是逐集合属性，不是全局开关。
- 正定向速度使表面沿该方向的反方向移动，与标量路径符号一致。
- RateSet 构造把 `calculateVisibility` 与「方向非零且定向速度非零」取与，因此零定向速度
  集合在原生侧不会计算可见性。

执行记录（`actualParameters`）：`mode = "rateSets"`、`nativeOverload`、
`rateSetCount`、`rateSets[]`（每项含 `requestedDirection`、`nativeUnitDirection`、
`directionalRateMagnitude`、`isotropicRateMagnitude`、带符号的
`nativeDirectionalVelocity`/`nativeIsotropicVelocity`、`maskMaterials` 与
`maskMaterialIds`、`maskSource = geometry | request`、实际 `calculateVisibility`），
以及顶层 `maskMaterials`/`maskMaterialIds`（首个集合）。`actualRate` 为 `null`
（没有单一速率），引擎仍为 `not-applicable`。

## 持久化与兼容

- 新定向步骤存为 schema 5；同一格式允许已有 schema 4 工艺步骤，因此可在一个配方中交错执行两种模型。
- 项目 JSON、请求哈希、实际原生参数、快照、VTK、历史结果、ZIP 导出及 replay project 保留方向、速率、遮挡、掩膜、材料和原生输出。
- 哈希使用归一化方向；全零步骤忽略方向和遮挡参数。旧格式 1–4 的哈希分支未改变。
- 省略 `rateSets` 的请求哈希与旧版本逐字节相同（`tests/test_directional.py` 固定了 2D/3D 标量与零速率哈希）。多集合请求逐集合单位化方向；`directionalVelocity = 0` 的集合忽略其方向与可见性，与标量规则一致。
- 这不是 ViennaPS Domain 保存格式；从已有 Domain 续算属于路线第 3 阶段。

## 材料速率表（materialRates）

定向步骤新增可选的材料速率表，对应安装版 4.6.2 的原生重载：

```python
ps.DirectionalProcess(
    direction,
    materialRates: Mapping[Material, (directional, isotropic)],
    defaultDirectionalRate=0.0,
    defaultIsotropicRate=0.0,
)
```

它与标量模式、`rateSets` 是三条并列的执行路径，不是对某个字段的补充。省略两个新字段时，
标量/`rateSets` 的 schema、请求哈希和原生调用逐字节不变。

注意 `materialRates` 在两种模型里同名但形状不同：`isotropic` 步骤是
`材料名 → 非负标量速率`，`directional` 步骤是
`材料名 → {directional, isotropic}`。两者属于不同步骤类型，不会在同一模型上混用；
消费方必须先看 `step.model` 再解析该字段。

### 字段与形状

```json
{
  "id": "step_directional_material",
  "name": "材料相关定向刻蚀",
  "enabled": true,
  "operation": "etch",
  "model": "directional",
  "direction": [0.0, -1.0, 0.0],
  "materialRates": {
    "Si": { "directional": 0.2, "isotropic": 0.01 },
    "SiO2": { "directional": 0.05, "isotropic": 0.02 }
  },
  "defaultMaterialRate": { "directional": 0.0, "isotropic": 0.0 },
  "duration": 0.4
}
```

| 字段 | 必选 | 约束 | 语义 |
| --- | --- | --- | --- |
| `direction` | 是 | 3 个有限分量，各在 [-1, 1]，非零；2D 归一化后第三分量必须为 0 | 与标量模式相同，执行前单位化 |
| `materialRates` | 否 | 材料名 → `{directional, isotropic}`；每个分量 0–2 µm/s | 按材料覆盖的速率表；名字来自 `/api/materials` |
| `defaultMaterialRate` | 否 | `{directional, isotropic}`，各 0–2 µm/s | 未列材料的回退值；省略等价于 `{0, 0}` |
| `duration` | 是 | 0.01–600 s | 与标量/`rateSets` 相同 |

`DirectionalMaterialRate` 的两个分量都是**非负速率大小**，不是带符号速度；缺省分量按 0
处理（例如只写 `{"directional": 0.2}` 等价于 `{"directional": 0.2, "isotropic": 0.0}`）。
表中的材料名是 JSON 对象键，重复名不可能出现；未知材料、运行时不可用的材料或超出范围
的分量都会被拒绝。

### 互斥与默认 0 保护

材料模式与以下字段互斥：

- `directionalRateMagnitude` / `isotropicRateMagnitude`（标量速率）；
- `rateSets`（多集合）；
- 步骤级 `calculateVisibility = false`。原生 RateMap 构造没有遮挡开关；
  该字段必须省略或保持 `true`，否则 422。

因此一个步骤只能是以下三者之一：

| 模式 | 识别方式 | 原生调用 |
| --- | --- | --- |
| 标量 | 没有 `rateSets` / `materialRates` / `defaultMaterialRate` | `DirectionalProcess(direction, dir_v, iso_v, masks, visibility)` |
| 多集合 | `rateSets` 非空 | `DirectionalProcess([RateSet, ...])` |
| 材料速率表 | `materialRates` 或 `defaultMaterialRate` 出现 | `DirectionalProcess(direction, RateMap, default_dir, default_iso)` |

**默认 0 保护**：`defaultMaterialRate` 省略时，未出现在 `materialRates` 里的材料两个分量
都是 0，不会被这一步移动；即使表里只列了一种材料，其他材料也保持不动。反过来，若要让
未列材料按某个速率移动，必须显式写 `defaultMaterialRate`。至少有一个表项或默认值的有效
速率大于 0，否则步骤被拒绝（`has_motion` 也识别材料模式，不会把它误判为零速率 no-op）。

### 操作符号（重要：与标量路径不同）

请求 JSON 里永远是非负大小；写负数会被 schema 拒绝。“刻蚀/沉积”由 `operation` 决定，
但原生 `DirectionalVelocityFieldSimple` 的两个分量使用**不同**的符号约定：

- `directional > 0`：沿 `direction` 方向移动；当 `direction` 指向表面内部时就是刻蚀。
- `isotropic > 0`：沿外法向生长，即沉积；负值才是刻蚀。

因此 worker 的映射是：

| operation | 原生 directional | 原生 isotropic |
|---|---:|---:|
| `etch` | `+directional` | `-isotropic` |
| `deposit` | `-directional` | `+isotropic` |

不要对两个分量统一乘 `-1`（那会让材料模式的“刻蚀”向上生长）。运行记录同时保留
请求值和两个带符号的原生值，并分别记录 `directionalSign` / `isotropicSign`。

### 2D / 3D

- 2D：`direction` 仍是 `[x, h, 0]`，按单位化后的方向检查第三分量；材料名和速率表的
  语义与 3D 相同。
- 3D：`direction` 是 `[x, y, z]`，`z` 为垂直方向。
- `operation: "deposit"` 仍要求 `depositMaterial`，并在原生过程前执行
  `duplicateTopLevelSet`；材料表可以引用该步骤自己的沉积材料，因为它在过程开始前已经
  由复制创建。其余材料必须来自初始几何或更早的启用沉积。

### 校验与拒绝条件

- 表项/默认值的每个分量必须在 `[0, 2]` 且有限；越界 → 422。
- 材料名必须在材料目录中，并且在该步骤运行时可用（初始几何或更早的启用沉积）；
  该步骤自己的 `depositMaterial` 例外。未知/不可用 → 422，路径为
  `steps[i].materialRates.<name>`。
- `direction` 缺失或全零 → 422；2D 第三分量非零 → 422。
- 与标量字段、`rateSets` 同时出现 → 422；`calculateVisibility = false` → 422。
- 所有有效速率都为 0（表为空且默认值 0，或全部为 0）→ 422；不会生成空的原生
  `RateMap`。
- 材料模式没有“每材料掩膜”字段：未列材料由默认值（缺省为 0）保护，而不是由
  `maskMaterials` 保护。这与 `rateSets` 的逐集合 `maskMaterials` 不同。

### 原生映射与执行记录

worker 的实际调用是：

```python
directional_sign = +1 if operation == "etch" else -1
isotropic_sign   = -1 if operation == "etch" else +1
ps.DirectionalProcess(
    native_unit_direction,
    {material_handle(name): (directional_sign * directional,
                             isotropic_sign * isotropic), ...},
    directional_sign * default_directional,
    isotropic_sign * default_isotropic,
)
```

原生 RateMap 的默认值对未列材料生效，因此默认 0 就是“保护未列材料”的原生表达。

`actualParameters` 记录：

- `mode = "materialRates"`、`nativeOverload`；
- `requestedDirection`、`nativeUnitDirection`、`directionCoordinateMap`；
- `materialRates[]`：每项的 `name`、`id`、请求的 `directionalRateMagnitude` /
  `isotropicRateMagnitude`、带符号的 `nativeDirectionalRate` / `nativeIsotropicRate`；
- `nativeMaterialRatesById`：材料 ID → `[nativeDirectionalRate, nativeIsotropicRate]`；
- `defaultMaterialRate`、`defaultMaterialRateOmitted`、`nativeDefaultDirectionalRate`、
  `nativeDefaultIsotropicRate`；
- `directionalSign` / `isotropicSign`；
- `masks = "material rate map only; unlisted materials use the default rate"`；
- `calculateVisibility = "not-applicable: native material map has no visibility control"`；
- `nativeProcessBackend = "CPU_DISK pinned; directional model has no stochastic flux engine"`；
- `raysPerPoint` / `randomSeed` 为 `not-applicable`。

`actualRate` 为 `null`（没有单一速率），`actualEngine` 仍为 `not-applicable`。
`step-configured` 事件的顶层字段与标量/`rateSets` 模式一致。

### 与 `RateSet` 的边界

`rateSets` 仍然是独立的多集合路径：每个集合有自己的方向、定向/各向同性速度、掩膜和
`calculateVisibility`，逐集合相加。材料速率表则是**单一方向 + 逐材料速率**，没有逐集合
方向、逐材料掩膜或可见性控制，不能与 `rateSets` 混用。需要“多方向束 + 每束自己的掩膜/
可见性”时用 `rateSets`；需要“同一方向下不同材料不同速率”时用 `materialRates`。

### 持久化与哈希

- 新字段只在出现时进入标准化 JSON 和请求哈希；省略时旧项目/旧 replay 哈希不变。
- 材料模式的方向参与哈希（与标量方向相同的单位化规则）；`materialRates` 表项和
  `defaultMaterialRate` 作为请求字段参与哈希。
- 项目保存/重开、运行历史、快照、VTK、ZIP 导出和 replay project 都保留材料名、材料 ID、
  请求速率、带符号原生速率、默认值、`directionalSign` 和 `isotropicSign`；没有新增端点或资产类型。
