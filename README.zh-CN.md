# Etch Workbench

面向 [ViennaPS](https://github.com/ViennaTools/ViennaPS) 工艺仿真的本机 Web 工作台。
可以搭建二维/三维结构、编排多步刻蚀与沉积配方、在 CPU 或 NVIDIA GPU 上运行、
在三维查看器中检查快照与粒子通量，并导出可精确重放的运行记录。

按单用户、仅回环地址设计：服务只绑定 `127.0.0.1`，数据全部保存在本机，不访问外部
接口。已在 **ViennaPS 4.7.0 / ViennaLS 5.10.0** 上验证，运行平台为 Linux；ViennaPS 4.6.2 /
ViennaLS 5.8.5 环境保留用于回放、历史运行以及 4.7.0 绑定缺失的能力（见下文运行时说明）。

[English](README.md)

## 目录

- [功能](#功能)
- [架构](#架构)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [使用工作台](#使用工作台)
- [HTTP API](#http-api)
- [配置](#配置)
- [资源限制与安全](#资源限制与安全)
- [测试](#测试)
- [故障排查](#故障排查)
- [仓库结构](#仓库结构)
- [文档](#文档)
- [已知限制](#已知限制)
- [许可](#许可)

## 功能

**结构编辑**

- 二维：平面与沟槽，支持侧壁倾角、掩膜高度与掩膜倾角。
- 三维：平面、沟槽与圆孔结构、Fin 挤出、带顶层开口的多层薄膜堆叠，以及 GDS
  版图导入（层到材料的映射与周期边界修复）。
- 15 种内置材料（`Si`、`SiO2`、`Si3N4`、`PolySi`、`SiGe`、`Al2O3`、`HfO2`、
  `TiN`、`W`、`Cu`、`PMMA`、`Mask`、`SiN`、`Metal`、`Dielectric`）和自定义材料，
  薄膜/掩膜角色可独立设置。
- schema 7 工程原生支持 µm 与 nm 两种长度单位；编辑器可在两者间切换，按比例换算
  长度与速率，时长、角度、方向向量保持不变。
- 运行前可预览二维/三维结构，并显示材料图例。

**工艺配方**

- 各向同性刻蚀与沉积。
- 射线追踪单粒子刻蚀/沉积（`singleParticle`），支持 CPU 与 GPU 通量引擎。
- 原生专用模型：SF6/O2、CF4/O2、HBr/O2、SF6/C4F8、含氟碳、离子束刻蚀、
  法拉第笼离子束刻蚀（实验，2D/3D，2D 仅限 CPU 通量引擎）、几何沟槽沉积（实验，2D/3D）、
  湿法刻蚀、TEOS 沉积、TEOS PECVD、ALD、氧化、氧化层再生长、选择性外延、
  中性粒子输运、多粒子。两个实验模型有真实原生构造与 CPU 冒烟测试，但还没有归档的
  Stage 8/9 直接参考验收行，因此运行时会附带验证警告。原始审计看到的 2D 法拉第笼
  不返回属于未固定引擎的 AUTO/GPU 内核；固定 CPU_DISK 后全部 cage/tilt 组合都能完成，
  因此 2D 只在 `solver.enginePreference` 为 `cpu` 时放行（AUTO/GPU 请求在启动 worker
  之前返回 422）。IBE 模型还要求 `thresholdEnergy < meanEnergy`；阈值等于或高于平均
  能量时原生产额符号反转，刻蚀步骤会变成生长。几何沟槽沉积在平面、沟槽、孔、鳍与叠层
  几何上支持 2D/3D，GDS 输入因缺少可界定的垂直范围仍被拒绝。
- 可选原生控制：选择性外延的 `nvFactors`、四个等离子体模型的
  `coverageTolerance`，以及 `advanced` 等离子体参数对象。
- 定向速率模型与选择性对照，并支持从原生脚本翻译时使用的原生速率符号模式。
- 几何步骤：planarize（显式高度或测量停止材料）、boolean / GDS boolean、
  insertBox、removeTopLevelSet、removeMaterial，以及支持显式/`allExcept` 掩膜
  和可选沉积材料的 particleShape。
- 步级 advection 覆盖（`spatialScheme`、`temporalScheme`、`timeStepRatio`、
  `maxDissipation`）。
- 步骤可启用/禁用、重排，并逐项设置原生参数。

**执行**

- 同一时刻只执行一个运行，在受监督的子进程中完成，带固定种子、墙钟超时、
  RSS 监视与取消回收。
- 引擎偏好 `auto` / `cpu` / `gpu`；显式选择 GPU 不会静默回退到 CPU。
  GPU 设备可通过稳定 UUID 固定。
- 实时状态、每步引擎/种子记录、可复现的输入哈希。
- 可选的 v2 执行注册表（`VIENNAPS_WORKBENCH_V2_EXECUTION=1`），按
  process / field / geometry 分发，逐步骤记录 plan/outputs/state，已认领步骤不会
  静默回退旧路径。

**结果**

- 快照查看器支持旋转、裁剪平面与正交截面。
- 表面通量、原生中间输出、CSV/PNG 导出。
- 按步骤下载产物 ZIP。
- 重放项目导入/导出、连续算包、项目保存与重新打开。
- 运行历史在服务重启后仍可读取，并支持
  `?run=<run-id>#legacy` 直接打开运行记录与 v2 plan/output/state。
- 原生 FinFET.py 验收 fixture：等价 schema-7 工程、原始 nm 参考与 µm twin
  的 surface/volume digest（`docs/finfet-example/`）。

## 架构

```
浏览器  (React 19 + vtk.js，由 Vite 构建)
   │  仅通过 127.0.0.1 的 HTTP/JSON
   ▼
FastAPI 服务  backend/main.py
   │  进程内 supervisor，单活动运行，带身份校验的生命周期
   ▼
worker 子进程  worker/simulate.py
   │  从系统 site-packages 导入 viennaps / viennals
   ▼
var/runs/<run-id>/   input.json、manifest.json、snapshots/*.vtk、flux/*.csv
```

Web 进程只读取包元数据和静态资源；ViennaPS/CUDA 原生库只在 worker 子进程中加载。
运行产物都是普通文件，离开原进程也能检查和重放。

| 路径 | 作用 |
| --- | --- |
| `backend/` | FastAPI 应用、校验、存储、supervisor、原生绑定 |
| `worker/` | 仿真入口与原生 `viennaps` 探针 |
| `frontend/` | React + TypeScript 界面与 vtk.js 查看器 |
| `scripts/` | 启动/停止、构建、宿主探测、迁移与验收脚本 |
| `tests/` | Python 单元与 API 集成测试 |
| `docs/` | 契约、schema、验收报告、能力矩阵 |
| `var/` | 运行时数据：项目、运行、Domain 状态、GDS 资源（git 忽略） |

## 环境要求

| 组件 | 要求 |
| --- | --- |
| 操作系统 | Linux（依赖 `/proc` 做进程监督与 CPU 亲和性读取） |
| Python | 3.12 |
| ViennaPS | 默认 4.7.0（`requirements-v470.lock`）；4.6.2 保留用于回放/历史运行。GPU 运行需要 CUDA 构建 |
| ViennaLS | 5.8.5 |
| Node.js | 20.19+ 或 22.12+（Vite 8） |
| GPU | 可选，NVIDIA GPU + CUDA，用于 `GPU_DISK` |

ViennaPS 与 ViennaLS **不是**本项目的 pip 依赖：工作台必须使用宿主已安装的引擎，
且不得升级或替换它。因此虚拟环境要用 `--system-site-packages` 创建。

## 安装

```bash
git clone https://github.com/concaveCat/etch-workbench.git
cd etch-workbench

# 1. 能访问系统 ViennaPS/ViennaLS 的 Python 环境
python3.12 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.lock

# 2. 前端产物（缺少时启动脚本会拒绝运行）
cd frontend
npm ci
npm run build
cd ..
```

确认虚拟环境能看到引擎：

```bash
.venv/bin/python -c "import viennaps, viennals; print(viennaps.__version__, viennals.__version__)"
# 预期输出：4.7.0 5.10.0（保留的 4.6.2 旧环境输出 4.6.2 5.8.5）
```

如果导入失败，请先安装 ViennaPS/ViennaLS（含 Python 绑定），再用
`--system-site-packages` 重建 `.venv`。工作台不负责编译或安装引擎。

## 快速开始

```bash
./scripts/start.sh     # 后台启动服务
./scripts/status.sh    # 输出地址与健康状态
./scripts/stop.sh      # 只停止它自己启动的进程
```

然后打开输出的地址，例如 <http://127.0.0.1:8789/>。

端口按以下顺序确定：

1. 环境变量 `WORKBENCH_PORT`；
2. `var/listen-port.json`（服务换端口时写入）；
3. 内置默认值 `8767`。

服务始终绑定 `127.0.0.1`；`Host` 头与当前回环地址/端口不一致的请求会被拒绝。
日志写入 `var/server.log`。

## 使用工作台

一次最小流程：

1. **结构** —— 保留默认 Si 沟槽，或选择平面/沟槽（二维）、圆孔/Fin/堆叠/GDS
   （三维），设置网格步长 `gridDelta` 与材料，点击**预览**。
2. **配方** —— 添加步骤，选择 `isotropic`、操作 `etch`、速率 `-0.5 µm/s`、
   时长 `0.4 s`。继续添加即可组成多步流程；禁用的步骤会保留在项目里但不执行。
3. **求解器** —— 设置时间步比例（默认 `0.25`）、粒子模型的每点射线数、CPU 线程数、
   随机种子模式、引擎偏好与超时。
4. **运行** —— 提交后观察状态。同一时刻只执行一个运行；上一个未结束或未取消时，
   新的提交会被拒绝。
5. **查看** —— 打开最新快照，旋转/裁剪/剖切结构、切换材料可见性，查看粒子步骤的
   通量或中间输出。
6. **保存/导出** —— 保存项目、下载运行 ZIP，或导出重放项目，稍后导入即可复现相同
   引擎与种子。

也可以直接调用 API：

```bash
curl -s http://127.0.0.1:8789/api/health
curl -s http://127.0.0.1:8789/api/projects
```

## HTTP API

所有端点都相对于回环地址，使用 JSON。错误返回 `{"detail": ...}` 与 `4xx` 状态码。
交互式 OpenAPI 页面被有意关闭；`docs/api-contract.md` 是规范契约。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务、Python 与引擎版本、活动运行 |
| `GET` | `/api/capabilities` | 已验证能力与资源策略 |
| `GET` | `/api/gpu-devices` | 可见 CUDA 设备与 UUID |
| `GET` | `/api/materials` | 内置与自定义材料目录 |
| `POST` | `/api/materials` | 新建自定义材料 |
| `PATCH`/`DELETE` | `/api/materials/{name}` | 修改或删除自定义材料 |
| `POST` | `/api/gds-assets` | 上传 GDS 版图（≤ 64 MiB） |
| `GET` | `/api/gds-assets/{id}` | 下载已存版图 |
| `POST` | `/api/gds-assets/{id}/inspect` | 检查层与边界 |
| `POST` | `/api/projects/validate` | 只校验、不保存项目 |
| `GET`/`POST` | `/api/projects` | 列出或新建项目 |
| `GET`/`PUT` | `/api/projects/{id}` | 读取或替换项目 |
| `POST` | `/api/runs` | 提交仿真运行（202） |
| `POST` | `/api/runs/flux` | 针对单个步骤做一次通量计算 |
| `POST` | `/api/runs/slice` | 提取表面切片 |
| `POST` | `/api/runs/replay` | 重放导出的重放项目 |
| `GET` | `/api/runs` | 列出运行 |
| `GET` | `/api/runs/{id}` | 运行 manifest 与步骤状态 |
| `POST` | `/api/runs/{id}/cancel` | 取消运行中的任务 |
| `GET` | `/api/runs/{id}/snapshots/{step}` | 以 JSON 获取快照网格 |
| `GET` | `/api/runs/{id}/flux-result` | 以 JSON/CSV 获取通量 |
| `GET` | `/api/runs/{id}/intermediate-outputs/{step}/{file}` | 原生中间文件 |
| `GET` | `/api/runs/{id}/artifacts/{step}` | 步骤产物元数据 |
| `GET` | `/api/runs/{id}/download` | 流式下载运行 ZIP |
| `GET` | `/api/runs/{id}/replay-project` | 导出重放信封 |
| `GET` | `/api/runs/{id}/continuation-project/{step}` | 构造连续算项目 |
| `POST` | `/api/continuation-package/export` | 导出连续算包 |
| `POST` | `/api/continuation-package/import` | 导入连续算包 |

## 配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `WORKBENCH_PORT` | `var/listen-port.json`，否则 `8767` | 绑定的回环端口 |
| `VIENNAPS_WORKBENCH_DATA_DIR` | `./var` | 项目、运行与资源的根目录 |
| `VIENNAPS_PATCHED_RUNTIME_DIR` | 未设置 | 可选的补丁版 ViennaPS 运行时（存在时由 `scripts/start.sh` 设置） |
| `VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR` | 未设置 | 补丁运行时的版本归档 |
| `WORKBENCH_GPU_TEST` | 未设置 | 设为 `1` 启用 GPU 集成测试 |
| `VIENNAPS_WORKBENCH_PROFILE` | 开 | 设为 `0` 关闭分段计时事件 |
| `VIENNAPS_WORKBENCH_LAZY_SNAPSHOTS` | 开 | 设为 `0` 时步骤快照在运行结束时立即写出，而不是首次读取时生成 |
| `VIENNAPS_WORKBENCH_GPU_CONTEXT_REUSE` | 开 | 设为 `0` 时在连续 GPU 步骤之间销毁 CUDA/OptiX context |
| `WORKBENCH_MAX_ARTIFACT_MIB` | `4096` | 单次运行的产物与归档/包上限（MiB，最小 64，无上限） |
| `WORKBENCH_MAX_DATA_GIB` | `10` | 应用数据总量上限（GiB，最小 1，无上限） |
| `WORKBENCH_MIN_FREE_GIB` | `20` | 接受新数据前要求的剩余磁盘空间（GiB，最小 0） |
| `WORKBENCH_RSS_LIMIT_MIB` | `8192` | 受监督运行的进程组 RSS 软监视阈值（MiB，最小 512） |
| `WORKBENCH_MAX_LOG_MIB` | `4` | 单次运行标准输出/错误合计上限（MiB，最小 1） |
| `VIENNAPS_DEBUG_NATIVE_CALLBACKS` | 未设置 | 输出详细的原生回调日志 |
| `WORKBENCH_HOST_VERIFY_SCRIPT` | 未设置 | `scripts/finalize-host.py` 可选调用的宿主自检脚本 |

## 资源限制与安全

- 每个服务实例**只允许一个活动运行**；再次提交返回 `409`。
- 运行在独立的会话/进程组中执行，并按进程组整体终止。
- 墙钟超时：每个项目 1–600 s（策略默认 120 s）。
- 每 200 ms 采样一次 RSS，默认超过 8192 MiB 时终止子进程
  （`WORKBENCH_RSS_LIMIT_MIB`，最小 512）。这是软性监视，不是硬性 cgroup
  上限，也不统计 CUDA 显存。
- 单次运行的标准输出/错误合计默认上限 4 MiB（`WORKBENCH_MAX_LOG_MIB`，最小 1）；
  产物上限默认 4 GiB（`WORKBENCH_MAX_ARTIFACT_MIB`，最小 64 MiB，无上限）。
  预检、快照物化与 ZIP 包使用同一个上限。
- 应用数据默认上限 10 GiB（`WORKBENCH_MAX_DATA_GIB`，最小 1）；文件系统剩余
  空间默认低于 20 GiB 时拒绝启动运行（`WORKBENCH_MIN_FREE_GIB`，最小 0）。
  调大产物上限不会绕过这两个保护。
- `GET /api/capabilities` 返回每个限制的有效值、默认值、最小值，以及取值来自
  环境变量还是默认值。
- 上传的 GDS 请求体上限 64 MiB。
- 服务不会操作其他进程：启动/停止依赖保存的 PID 与进程启动 tick，因此 PID 被
  复用时也不会误杀。

## 测试

```bash
# 单元与 API 集成测试（默认跳过 GPU 用例）
.venv/bin/python -m unittest discover -s tests -v

# 在 CUDA 宿主上启用 GPU 集成用例
WORKBENCH_GPU_TEST=1 .venv/bin/python -m unittest discover -s tests -v

# 前端类型检查与生产构建
cd frontend && npm run typecheck && npm run build
```

如果工作台服务已经在使用 `./var`，测试会在毫秒级失败并报
`another Etch Workbench server already holds the data-directory lock`。
此时把 `VIENNAPS_WORKBENCH_DATA_DIR` 指向一个**仓库内**的新目录（例如
`$PWD/var/test-$(date +%s)`），并换一个运行中服务未占用的端口
（`WORKBENCH_PORT=8767`）。目录必须留在仓库内，因为 `scripts/bounded_child.py`
会拒绝仓库之外的 `--record`/`--log-path`。

`frontend/tests/*.mjs` 是真实浏览器验收脚本（Playwright），需要已启动的服务和
准备好的数据目录；它们面向宿主验收环境而非通用 CI，改造测试套件时可作为参考。

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 启动时报 `frontend build is missing` | 在 `frontend/` 下执行 `npm ci && npm run build`。 |
| `cannot bind configured loopback port` | 端口被其他进程占用。设置 `WORKBENCH_PORT` 或删除 `var/listen-port.json`；工作台不会杀掉未知监听进程。 |
| `ModuleNotFoundError: viennaps` | `.venv` 未使用 `--system-site-packages`，或该 Python 未安装 ViennaPS。 |
| GPU 运行被拒绝或 `gpuAvailable()` 为 false | 检查 `nvidia-smi`、ViennaPS 的 CUDA 构建，以及 `GET /api/gpu-devices`。仅开放 `docs/stage9-evidence/gpu-capability-matrix.json` 中记录过的 `GPU_DISK` 组合；schema-7 的 `solver.gpuEngine=GPU_TRIANGLE` 只开放两条已归档 sf6o2 行，且不会回退。 |
| `request Host must be the active loopback address` | 使用 `127.0.0.1:<端口>` 或 `localhost:<端口>`；反向代理与自定义域名会被有意拒绝。 |
| 运行因 `run artifact size exceeded` 停止 | 单步产物超过有效上限（默认 4 GiB，可用 `WORKBENCH_MAX_ARTIFACT_MIB` 调整）；降低网格分辨率、射线数或输出频率。 |

## 仓库结构

```text
backend/        FastAPI 应用、模型、校验、存储、supervisor
worker/         simulate.py 入口、原生探针、性能分析工具
frontend/       React/TypeScript 界面、vtk.js 查看器、Playwright 验收脚本
scripts/        启动/停止/状态、前端构建、宿主探测、迁移
tests/          Python unittest 套件、fixtures、旧格式读取器
docs/           契约、schema、能力矩阵、验收报告
var/            运行时数据（git 忽略）
```

## 文档

- `docs/api-contract.md` —— 线协议、哈希、重放信封。
- `docs/schema-project-*.json`、`docs/schema-replay-*.json` —— 项目与重放 schema。
- `docs/capabilities.json`、`docs/resource-policy.json` —— 已验证的原生行为与限制。
- `docs/ACCEPTANCE_STAGE*.md` —— 逐阶段验收报告。
- `docs/performance-profile-20261029.md` —— GPU/CPU 耗时分解。
- `docs/compatibility.md` —— 固定环境与原生失败记录。

体积较大的验收证据（`docs/*-evidence/`）有意不入库；运行时必需的三份能力矩阵是例外。

## 已知限制

- 已在 ViennaPS 4.7.0 / ViennaLS 5.10.0（默认）与 ViennaPS 4.6.2 / ViennaLS 5.8.5（保留）
  上验证，其他版本会被拒绝而不是猜测兼容。归档的能力矩阵仍固定为产出它们的 4.6.2 运行。
- GPU 仅开放覆盖组合的 `GPU_DISK`；schema-7 的 `solver.gpuEngine=GPU_TRIANGLE`
  只开放两条已归档的 2D/3D sf6o2 刻蚀/reflective stack 行，其余组合一律拒绝；
  `GPU_LINE` 不开放。
- 两个运行时的 GPU MultiParticle 绑定都只转换一种离子：4.6.2 原版与 4.7.0 原版都只输出
  一个 `ion<label><0>` 通量场，速率回调只收到长度为 1 的通量向量。多离子 GPU 运行因此
  必须使用打过补丁的 ViennaPS 4.6.2 运行时；在 4.7.0 上会直接拒绝并指明运行时，而不是
  产出静默错误的几何。多离子 `CPU_DISK` 在 4.7.0 上可用。
- ViennaPS 4.7.0 封禁三个 4.6.2 没有的原生回归：ALD `surfaceDiffusion` 卡死、高级氧化
  控制项失效、2D `FaradayCageEtching` 零刻蚀。每项都会连带字段、运行时与实测原因一起
  拒绝；4.6.2 运行时仍可执行。
- 宿主能力证据来自一张 NVIDIA RTX 3060 Laptop GPU；多 GPU 选择已实现并有单元测试，
  但未在两张物理卡上验证。
- 界面是单用户本机工具：没有认证、没有 TLS、不提供远程访问。
- 浏览器验收脚本依赖宿主工具链，不是封闭可移植的测试。

## 许可

尚未选择许可证。在添加许可证之前，源码默认保留所有权利。如需复用，请先开 issue
讨论许可（MIT 或 Apache-2.0 是这个代码库比较自然的选择）。
