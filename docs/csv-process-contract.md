# CSV 速率表合同（schemaVersion 7 / `csvProcess`）

本合同描述上传 CSV 速率表与 `csvProcess` 步骤的实际行为，全部基于安装版本
ViennaPS 4.6.2 的真实绑定、`ps.RateGrid` 的直接探测，以及
`tests/test_csv_process.py` 的验收；不是从更新源码推断的能力。

## 1. 原生事实（已实测）

| 事实 | 验证结果 |
|---|---|
| `ps.CSVFileProcess` 在 2D/3D 都存在 | `ps.setDimension(2/3)` 后顶层 `ps.CSVFileProcess` 分别绑定到 `viennaps.d2/d3` |
| CSV 列数 = 维度数 D | 2D 为 **`x,rate`**（2 列）；3D 为 **`x,y,rate`**（3 列）。**没有 z 列** |
| 3D 速率只随横向 x/y 变化 | `RateGrid<D=3>` 用 `pt[0], pt[1]` 建表，`interpolateLinear` 只读 `coord[0]/coord[1]` |
| 2D 只有一条横向轴 | `RateGrid<D=2>` 用 `coord[0]` 查表；`offset[1]` 在 2D 中不参与计算 |
| 插值模式 | 原生枚举 `LINEAR / IDW / CUSTOM`；`loadFromCSV` 会按表结构自动判断，但工作台要求显式选择，并在构造后调用 `setInterpolationMode` |
| `setCustomInterpolator` | 接受任意 Python 可调用对象；工作台没有任何字段能到达它，`CUSTOM` 也在 schema 层被拒绝 |
| 方向向量 | 原生构造时单位化；零向量会在原生 `normalizeVec3D` 抛错 |
| 速率符号 | 原生模型没有 etch/deposit 参数。方向 `[0,-1,0]`（2D）或 `[0,0,-1]`（3D）时，正速率使表面外移（沉积），负速率刻蚀 |
| offset | 原生在 `coordinate + offset` 处查表；把 offset 设为 `[1,0]` 等价于把表整体向负 x 平移 1 µm |
| `calculateVisibility` | 原生用 `CalculateVisibilities` 计算遮挡；关闭后不计算 |
| 掩膜 | 构造函数 `maskMaterials` 中的材料速度为 0；工作台复用 Directional/粒子路径的 `protected_material_names`（trench/hole/fin 的 `maskMaterial`、stack 的 mask 层、legacy stack `gdsMask` overlay（`protectsEtch=true`）、GDS 的 `mask` 层），不新增材料 |
| 加载失败 | `loadFromCSV` 失败时原生只打印错误，之后速度恒为 0；BOM、tab、Unicode 空白、内联 `#` 等输入会让原生解析结果与 Python 不同，LINEAR 甚至可能崩溃。因此工作台只存储规范化后的表，并在 worker 里再次校验 |
| 可见性与多层 level set | 原生 `VelocityFieldFromFile::prepare` 只在最顶层 level set 上计算 visibility，并把同一套 point ID 用于其他 level set。对独立 mask overlay，关闭 `calculateVisibility` 才能可靠地让下层薄膜运动；掩膜本身仍保持静止 |

2D/3D 直接探测（1 s、恒定 0.5 µm/s、方向 -h/-z）都得到 **+0.5 µm**；
负速率得到 **-0.5 µm**。2D/3D 的 reflective 与 periodic 边界都完成，无崩溃。

## 2. 规范化后的资产

原生 reader 只去除 ASCII 空格、不识别 BOM、把 tab 当数据，并且只有“行首（原生空格裁剪后）就是 `#`”才算注释。
工作台不复制这些怪癖，而是在上传时严格解析并重新输出一份原生安全的规范表：

| 输入 | 处理 |
|---|---|
| 文件开头 UTF-8 BOM | 去除；文件中其他位置出现 BOM → 422 |
| 非 ASCII（Unicode 空白、Unicode 数字等） | 422（原生 `strtod`/reader 不接受） |
| 行首/行尾 ASCII 空格、tab | 归一化；数值单元格两端的空格/tab 去除 |
| 空行、纯空白行 | 删除（原生 reader 本身不接受空行，规范表不会包含） |
| 注释行（行首 ASCII 空白后是 `#`） | 整行删除；第二个 `#` 属于注释内容 |
| 数据行内的内联 `#` | 422（原生会把它当数值解析） |
| `nan`/`inf`/十六进制/下划线/非有限值 | 422 |
| 下溢为 0 的非零字面量（如 `1e-400`） | 422；显式 `0` 仍接受 |
| 非零 subnormal（\|value\| < `sys.float_info.min`，如 `1e-308`/`5e-324`） | 422；原生 reader 拒绝 subnormal，IDW 会静默不动、LINEAR 可能崩溃。`1e-307` 是 normal；最小 normal 约为 `2.2250738585072014e-308`，低于它的非零值仍会被拒绝 |
| 数值格式 | 用 Python 最短往返十进制重新输出（如 `1.0e-7`、`0.5`、`-2.0`） |
| 行尾 | 规范为 LF，末尾单个换行 |

规范表每行恰好 D 列、纯 ASCII，`assetId = SHA-256(规范字节)`，存储的 `.csv` 就是这份规范字节。
worker 会再次 canonicalize 其 run 目录副本；只要字节不是规范形式就显式失败，绝不让 BOM/tab 表进入原生 `RateGrid`。
**旧版本（未规范化）资产需要重新上传**；直接引用旧资产会得到 422。

## 3. 资产与 API

资产类型：`csv-assets`，内容寻址，存放在 `VIENNAPS_WORKBENCH_DATA_DIR/csv-assets/`。

| 端点 | 行为 |
|---|---|
| `POST /api/csv-assets?file_name=<name>&dimension=2\|3` | `text/csv` 或 `application/octet-stream`，流式读取并限制 **16 MiB**；严格校验并规范化（见 §2）；以规范字节的 SHA-256 命名存储；写同名 `.json` 元数据；返回 `assetId` 与摘要 |
| `GET /api/csv-assets/{assetId}` | 重新解析规范表并返回元数据与摘要；SHA-256 不匹配或存储字节非规范返回 404 |

元数据（`<assetId>.json`）字段：

```jsonc
{
  "assetId": "<64 hex sha256 of canonical bytes>", "fileName": "rates.csv",
  "schema": "viennaps-rate-grid-v1", "canonicalFormat": "ascii-lf-comma-decimal-v1",
  "canonicalSha256": "<same as assetId>", "sourceSha256": "<sha256 of uploaded bytes>",
  "dimension": 2, "columns": ["x", "rate"], "rowCount": 5,
  "coordinateUnit": "um", "rateUnit": "um/s",
  "x": {"min": -2, "max": 2, "count": 5},
  "y": {"min": -2, "max": 2, "count": 3},      // 仅 3D
  "rate": {"min": 0.2, "max": 0.6, "sign": "deposit"},
  "linearCompatible": true, "idwCompatible": true,
  "sizeBytes": 60, "sourceSizeBytes": 72
}
```

项目只保存 `assetId` + `fileName`（`fileName` 禁止 `/`、`\`、换行/NUL/DEL 等控制字符），从不保存路径。

## 4. 步骤模型

```jsonc
{
  "id": "step_csv_0001", "name": "CSV 速率表", "enabled": true,
  "operation": "native",                        // 固定；符号由 CSV 速率决定
  "model": "csvProcess",
  "duration": 1.0,                              // 0.01–600 s
  "assetId": "<64 hex>", "fileName": "rates.csv",
  "direction": [0, -1, 0],                      // 3D 为 [x,y,z]；2D 第三分量必须为 0
  "offset": [0, 0],                             // 2D 第二分量必须为 0
  "isotropicComponent": 0.0,                    // 0–100
  "directionalComponent": 1.0,                  // 0–100；两者不能同时为 0
  "interpolationMode": "LINEAR",                // 必填；LINEAR | IDW
  "idwNeighbors": 4,                            // 仅 IDW；1–64，且不得超过表行数
  "calculateVisibility": true
}
```

验证在 worker 启动前完成：

- 资产存在且 SHA-256 匹配；存储字节必须是规范形式（否则提示重新上传）；元数据维度必须等于项目维度；
- 表结构与维度匹配（2D 2 列 / 3D 3 列），所有数值有限、非 subnormal 且规范；
- `LINEAR`：2D 要求 x 严格递增；3D 要求完整矩形 x/y 网格（至少 2×2，不能缺格）；
- `IDW`：`idwNeighbors <= rowCount`；
- 方向非零，2D 的第三个分量为 0，2D 的 `offset[1]` 为 0，offset 在 ±1e6 µm 内；
- `interpolationMode` 缺失、`CUSTOM`、未知字段（例如 `customInterpolator`）都得到 422。

覆盖率 warning 按**原生查表范围**计算：x/y 的检查区间是 `[-half + offset, half + offset]`，
不是未平移的 `[-half, half]`。原生在 LINEAR 下会外推/钳制，IDW 始终用最近样本。

## 5. Worker 行为

1. supervisor 把每个被引用资产复制到 run 目录的 `csv-source-<assetId>.csv`，复制后再次校验 SHA-256；
2. worker 只从 `input.json` 的 `csvAssets` 映射取 run 内路径，路径逃出 run 目录即失败；
3. worker 重新 canonicalize 副本并确认字节完全相同，然后构造真实 `ps.CSVFileProcess(path, direction, offset, iso, dir, masks, calculateVisibility)`；
4. 显式调用 `setInterpolationMode`（IDW 时再调用 `setIDWNeighbors`）和 `setOffset`；
5. 运行记录写入 `actualParameters.csvProcess`：资产 id/文件名/SHA-256、规范格式、实际单位方向、offset、两个分量、插值模式、邻居数、掩膜材料与 id、CSV 行数/范围/符号、时长、`actualEngine: not-applicable`。

引擎固定为 CPU 路径（`FluxEngineType.CPU_DISK`）；CSV 模型没有随机射线/通量引擎，也没有 seed。
任何缺失资产、哈希不符、非规范表、表不兼容或维度不符都会让 run 显式失败，不会退回零速度场。

## 6. 已验证维度与操作

| 项目 | 结果 |
|---|---|
| 2D | 平面、带 Mask 的 trench、带 Mask 的 Fin、负速率刻蚀、非零 offset、iso+dir 混合、关闭 visibility 均通过直接参考比较 |
| 3D | 平面 LINEAR、完整/不完整网格 IDW、stack 顶层 GDS mask、film-only legacy `gdsMask.protectsEtch` overlay 均通过 |
| 操作 | 固定 `native`；正速率沉积、负速率刻蚀，由 CSV 符号决定 |
| 引擎 | `not-applicable`（CPU 进程；不使用 GPU/射线通量） |
| 直接参考误差 | 2D/3D 均为 0（容差断言 1e-6 µm） |

## 7. 限制

- 3D 速率表只有 x/y 两列，不能表达随 z 变化的速率；这是安装版 `RateGrid<D=3>` 的实际语义。
- 2D 的 `offset[1]` 在原生实现中完全不参与计算，因此 schema 要求它为 0，避免静默忽略。
- LINEAR 的边界行为由原生决定：2D 超出最后一个 x 时取最后一个样本；3D 在 x/y 边界附近钳制到边缘单元。
- `CUSTOM` 与 `setCustomInterpolator` 永久不可达（任意 Python 会破坏请求哈希、回放和 bounded-child 模型）。
- 原生 `calculateVisibility` 的 visibility 数组只来自最顶层 level set；在有独立 mask overlay 的多 level set 结构上，
  建议关闭 `calculateVisibility`，否则下层薄膜的 velocity 可能被错误置零。掩膜保护本身不依赖这项。
- 便携包（`/api/continuation-package/export`）暂不打包 CSV 资产，因此含有 `csvProcess` 的项目导出会被明确拒绝（422）；本机保存、重开与 replay 正常。运行归档 ZIP 会包含 `assets/<assetId>.csv`。
- 未归档 GPU 覆盖；该模型本来也没有 GPU 路径。
