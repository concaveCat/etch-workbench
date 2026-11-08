# Stage 9 验收：GPU 能力矩阵与整体交付

## 环境和范围

- 安装版本：ViennaPS 4.6.2、ViennaLS 5.8.5；GPU：NVIDIA GeForce RTX 3060 Laptop GPU。
- GPU 原生探测、产品 worker 和独立 ViennaPS 参考均经过 `scripts/bounded_child.py`，保留超时、RSS/日志限制及子进程回收。
- 本阶段只把通过实测的 GPU_DISK 组合开放给 schema 7。schema 1–6 的既有项目格式、哈希、历史和重放继续由兼容回归覆盖。

## 能力矩阵和选择规则

`docs/stage9-evidence/gpu-capability-matrix.json` 现有 560 条证据行（558 条 `GPU_DISK` + 2 条 `GPU_TRIANGLE`），由 `tests/fixtures/derive_stage9_gpu_matrix.py` 从已验证证据生成，并逐行校验产品、直接 ViennaPS 参考及 bounded-child guard。每个发布签名都比较坐标、拓扑、材料数组和材料映射；材料标识仅在直接材料矩阵覆盖范围内泛化：

- 352 个 ViennaPS 专用模型行，涵盖实测平面、沟槽、圆孔、Fin、层叠结构及 GDS 路由；
- 90 个通用 `singleParticle` 行与 95 个 `multiParticle` 行，涵盖平面、沟槽、圆孔、Fin、GDS 及保留的层叠结构；
- 64 个 GDS GPU_DISK 能力行：24 个通用单/多粒子行，加 40 个专用工艺模型行。专用行覆盖 10 个工艺模型 × 2D/3D × 反射/周期边界，限定为独立产品和 ViennaPS 直连均通过的双图层签名；TEOS/PECVD 使用不同的 SiO₂ 沉积材料与 Si₃N₄ 掩膜材料。另有 300 个通用 GDS 材料直接对照案例：13 种内置材料的薄膜、掩膜和沉积角色，以及自定义原生 ID 177 和 240 的薄膜、掩膜、沉积用途。独立的 80 个 GPU_DISK 产品与 ViennaPS 直接对照案例验证了双图层通用角色和材料身份；另有 32 个案例验证 64 个通用映射上限，交叉覆盖 2D/3D、反射/周期、单/多粒子、刻蚀/沉积及 3D 薄膜/掩膜/混合角色。该 64 层矩阵使用 0.1 µm 网格与网格对齐的 GDS 原点，证据见 `docs/stage9-evidence/gpu-coverage-audit/gds-layer-count/grid-aligned-0.1/summary.json`。

此前发现的 8 个 periodic GDS 崩溃签名（SF₆/O₂、HBr/O₂、SF₆/C₄F₈、neutralTransport × 2D/3D）已修复并复验。根因是 ViennaPS 4.6.2 的 GDSGeometry 从布局边界推导周期；较小或偏移的 GDS 布局会覆盖项目中指定的周期范围，导致周期长度与 Domain 网格不一致。现在工作台在已选择的 GDS 临时副本中加入一个未映射材料的隐藏支撑图形，将 ViennaPS 原生边界对齐到项目周期范围；所选材料 Level-set 不包含该支撑层。GPU_DISK 与 CPU_DISK 各 40 组产品 worker/独立 ViennaPS 对照均通过，另有单层、偏移单层和仅掩膜 GPU 配置的 24 组 periodic 对照通过。超出请求周期范围的 GDS 放置会在原生运行前明确报错，要求扩大 Domain 或调整放置。周期修复的实时 Chromium/GPU 运行证据见 `docs/stage9-evidence/gpu-coverage-audit/browser/gds-periodic-repair/`；原始崩溃和 GDB 记录保留在 GDS 专项证据目录作历史诊断。

每行记录边界、几何类型、图案、材料集合、材料角色、专用材料绑定、实际引擎及产品/direct-reference 证据。具备材料范围证据的行允许切换到同一已验证内置 ID 集合或自定义 ID 177–240；MultiParticle 仍要求粒子组成及原生物理参数匹配，实测材料表可单独变化。已安装 ViennaPS 4.6.2 GPU MultiParticle 绑定只转换首个离子类型并忽略额外离子；同输入单次通量结果显示 GPU 缺少 CPU_DISK 返回的 `xenonIon` 场。多离子 2D/3D 的 8 个 GPU 产品/直连用例均失败；相同四类多离子配置的 8 个 CPU_DISK 产品/直连用例均通过。API 对多离子 GPU 返回具体原因，项目仍可导入并切换 `auto`/CPU，GPU 执行被阻止。限制和证据记录在 GPU 矩阵的 `knownEngineLimitations` 中。schema 7 重放还会拒绝与项目引擎偏好冲突的 CPU/GPU 要求；GPU 请求失败不会静默切回 CPU。

### 预设几何与 Fin 崩溃回退

- 每个 10 个专用通量模型覆盖 8 个基础预设路由：2D/3D 平面、2D/3D 沟槽、3D 圆孔、2D Fin、3D Fin 直接构造、3D Fin 挤出，共 80 个 GPU_DISK 组合；另有 30 个带掩膜的专用沟槽/圆孔组合。
- Boolean Fin 经过独立参考验证：60 个专用模型组合（10 模型 × 3 个维度/构造模式 × 带/不带掩膜），24 个通用单/多粒子组合（两种操作、3 个维度/构造模式、带/不带掩膜）。Fin、沟槽、圆孔的遮罩输入在真实 Domain 中保留 Mask 材料映射。
- 调试发现 ViennaPS 4.6.2 的原生 `d2.MakeFin` / `d3.MakeFin` 与 `sf6o2`、`hbro2`、`sf6c4f8`、`neutralTransport` 组合会导致进程 SIGSEGV；GPU_DISK 有 13 个失败签名，CPU_DISK 对其中 10 个标准签名也以同样方式失败，独立直接调用同样失败。原生堆栈符号不足，暂时无法定位库内具体 C++ 函数。
- 项目仍保存请求的 `construction: native`，以维持旧项目格式和哈希；运行时在这些模型启用时选择 ViennaPS `MakePlane + GeometryFactory + BooleanOperation + material level set` 生成 Fin。回退后的实际构造和模型写入 `geometryReadback`。24 个未遮罩的 CPU/GPU 组合以及 12 个 GPU 遮罩组合均与独立直接 ViennaPS 结果匹配；重放重复相同构造。
- 证据、输入、进程 guard、日志和逐网格比较位于 `docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/`；汇总在同目录 `coverage-report.json`。产品到真实 UI 的验收在 `docs/stage9-evidence/gpu-coverage-audit/browser/fin-auto-fallback-live.json`。

候选目录曾把部分旧产品输出留在临时 `var` 中。本次将被矩阵引用的旧产品目录和浏览器运行 guard 打包至 `docs/stage9-evidence/gpu-coverage-audit/legacy-products/`；`portable-product-evidence-manifest.json` 记录每个文件的大小和 SHA-256。重新生成矩阵不依赖用户数据目录或临时绝对路径。

主机 GPU 探测覆盖 2D GPU_DISK 刻蚀/沉积，以及 3D 反射/周期边界下的刻蚀/沉积。记录位于 `var/evidence/gpu-host/2d-product.guard.json`、`var/evidence/gpu-host/2d-product-probe/gpu-engine-results.json` 和 `var/evidence/gpu-host/summary.json`。

### 本机计算 GPU 选择与 RTX 3060 性能验收（2026-10-28）

- `GET /api/gpu-devices` 在 `bounded_child.py` 内枚举 `nvidia-smi` 报告的 CUDA NVIDIA 设备，返回名称、稳定 UUID、PCI 地址和显存。schema 7 的“计算 GPU”控件可选自动偏好或具体 UUID；自动偏好选显存最大的可见设备。显式设备 ID 纳入请求哈希，旧项目未设置 ID 时保持原哈希；项目保存/重开、导出/导入和重放保留同一 UUID。
- 产品 worker 在导入 ViennaPS 前按选中 UUID 设置 `CUDA_VISIBLE_DEVICES`，随后从 CUDA Runtime 读回 UUID/PCI，并确认 `nvidia-smi` 将实际 worker PID 列在该 UUID 下。运行历史也保存该读回；设备不匹配或显式 GPU 失败会报错，不会换卡或静默回退 CPU。自动模式对已验证 GPU 签名优先使用默认/选定设备；没有设备时仅在 CPU 对应签名已验证的情况下才可走 CPU。
- 本机枚举到一张 NVIDIA GeForce RTX 3060 Laptop GPU（UUID `GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d`，PCI `0000:01:00.0`，6 GiB）。真实 Chromium 验收先保持 GPU/引擎选择为“自动”，通过页面执行并读回实际 GPU_DISK 与 RTX UUID，再检查保存重开、显式 UUID 运行、重放固定设备和初始/最终网格对照。输入、worker 结果、guard 和报告在 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser/`。
- 另以同一真实 3D Si 平面刻蚀输入比较 CPU_DISK 与 GPU_DISK：160×160 横向单元，256 射线/点，固定种子 271828，20 个 CPU 线程，网格 `0.04 µm`，步长比 0.25，持续时间 0.2 s；每个引擎各 1 次预热、5 次顺序测量、不重叠。只计 ViennaPS `Process.apply`：GPU 中位数 1.571 s，CPU 中位数 4.513 s，中位加速 2.873×，bootstrap 95% 区间 2.446–3.020×。门槛为中位加速至少 1.5×、区间下界高于 1.2×，均通过。
- 两个引擎的每个产品样本分别与同引擎独立 ViennaPS 直调一致，且 CPU 与 GPU 都实际改变了表面。两种引擎的最终离散三角网格并非逐点相同：点数 37,258/37,313，三角形 73,735/73,862；材料集合与材料映射相同，最终点云最大对称最近顶点距离 0.020003 µm（网格间距 0.04 µm）。因此性能报告不将 CPU 与 GPU 不同三角剖分误报为完全相同；产品对各自引擎的独立参考则要求坐标、拓扑和材料数组完全一致。完整报告、样本、日志和每个 bounded-child guard 位于 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/performance/`。
- 单元测试用双 UUID 合成设备清单验证明确切换到第二个设备、未知设备 fail-closed 以及哈希/旧自动请求兼容；本机只有一张 CUDA 卡，故另一张实体卡之间的实机切换尚无从验证。真实 Chromium 验收在页面加载前拦截 `HTMLCanvasElement.getContext`，确认 VTK.js 实际为 3D WebGL2 画布传入 `powerPreference: high-performance`，并读回上下文属性和 renderer 字符串。该 headless Chromium 实际返回 SwiftShader 软件渲染器，不能据此声称浏览器硬件渲染使用 RTX 或集成 GPU。浏览器只能提出高性能偏好，最终显示设备仍由浏览器和驱动选择，与 ViennaPS CUDA 计算卡选择独立；原始读回及 runner guard/log 在 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser/acceptance.json` 和 `renderer-selection-browser-runner.{guard.json,log}`。
- 本轮重建前端后，以真实 Chromium 重跑设备选择/保存导入/重放，CUDA 自动默认和显式 RTX 选择均通过，页面错误为零；Stage 9 专项单元测试 22/22 通过，全量 `unittest discover -s tests` 为 93 项通过、4 项默认 GPU 用例跳过。全量日志 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/full-regression-post-gpu-renderer-acceptance-2026-10-28.log`；跳过的宿主 GPU 行由本节记录的真实 GPU 浏览器、样本基准和 545 行产品/direct-reference 能力证据覆盖。

## 真实网页验收

- `frontend/tests/stage9-live.mjs` 在 Chromium 中输入 SF6/O2 工艺，先改变基底材料角色并确认 GPU 选项被禁用，再恢复已测配置并执行 GPU_DISK。产品初始/最终网格与独立直接 ViennaPS 参考在坐标、连接关系、材料数组和材料映射上完全一致；保存、导出重放、重放执行、停止/启动/状态检查及服务重启后的历史重载均通过。运行 `a9f554f670f33ac6a3f91e51`，重放 `c1dfb3d9190e75fbfc9e8678`。证据：`docs/stage9-evidence/browser/browser-gpu-live.json` 及同目录输入、日志、guard、直接参考文件。早期服务前置条件和 Playwright `<option>` 断言的调试记录见 `docs/stage9-evidence/browser/debug-notes.md`。
- Stage 7 MultiParticle GPU 浏览器回归在 2D/3D 沟槽层叠上通过；粒子过程、通量、重放、项目保存/打开、结果历史和零速率 no-op 均与直接 ViennaPS 对照。过程和通量使用 GPU_DISK，浏览器无页面错误、控制台错误或失败请求。运行 ID 与输入/输出见 `docs/stage9-evidence/browser-multiparticle/browser-live.json` 和 `docs/stage9-evidence/browser-multiparticle/{2d,3d}/`。
- `frontend/tests/stage9-multiparticle-gpu-gate.mjs` 使用真实 Chromium 和候选 API 验证 GPU 选项会随粒子配置变化收紧：已测 1 离子 + 1 中性粒子配置开启，UI 操作实际构造单中性粒子和 8 离子两种未测组合后均禁用 GPU。证据：`docs/stage9-evidence/browser-multiparticle/gpu-gate-live.json`；API 同时拒绝这两种未测 GPU 项目。schema 7 重放的 CPU/GPU 偏好冲突由 API 自动测试拒绝。
- `frontend/tests/stage9-gds-gpu-live.mjs` 的 Chromium 验收覆盖单层和双层 GDS，并增加 64 映射 3D periodic 输入；UI 提交层编号、材料与掩膜标志后实际运行 GPU_DISK，产品初始/最终坐标、拓扑、材料数组与直接 ViennaPS 一致。报告为 `docs/stage9-evidence/gpu-coverage-audit/browser/gds-gpu/acceptance.json`。
- `frontend/tests/fin-gpu-fallback-live.mjs` 在 Chromium 中实跑 2D SF6/O2 Fin：UI 请求 `construction: native`，产品因 ViennaPS 4.6.2 的 `MakeFin` SIGSEGV 缺陷使用已验证 Boolean Fin，仍实际运行 GPU_DISK；初始/最终网格、保存重开和重放均与直接参考一致。报告为 `docs/stage9-evidence/gpu-coverage-audit/browser/fin-auto-fallback-live.json`。
- 上述浏览器运行通过项目 `scripts/start.sh`、`scripts/status.sh`、`scripts/stop.sh` 管理；新增 GPU 粒子签名后，真实 Chromium 验证已测 1 离子 + 1 中性粒子配置启用 GPU，而单中性粒子和 8 离子配置都会禁用 GPU。隔离候选服务在测试完成后停止。正式 8769 服务未操作。

## 2D/3D 分辨率扩展

- 后端、UI、GDS 检查、快照模型和静态 JSON Schema 已移除固定网格间隔数、表面点/三角形数和 32 MiB 快照 JSON 上限；网格间距只要求大于零且不超过 0.5 µm。旧格式的结构、哈希和重放格式未更换。
- 真实 GPU_DISK 产品 worker 与独立直接 ViennaPS 输出逐项匹配坐标、拓扑、材料数组和 level-set 材料映射。2D 使用 `gridDelta=0.005 µm`、x 宽 40 µm（8,000 横向间隔），24,003 点和 24,000 线单元；3D 使用 `gridDelta=0.02 µm`、6×6 µm（每轴 300 间隔、90,000 横向网格点），提取 271,803 个表面点和 540,000 个三角形。3D 每份快照约 50.6 MB，超过原 32 MiB 拒绝阈值。产品峰值观察 RSS 约 1.07 GB，独立参考约 1.34 GB。
- Chromium 在端口 8797 的隔离候选服务中通过真实页面导入高分辨率 2D/3D 项目、提交运行并核对 `/api/runs` 输入、GPU_DISK 实际引擎和直接参考。浏览器 2D 用例为 0.005 µm / 40 µm；浏览器 3D 用例为 0.02 µm / 2.6×2.6 µm，130 间隔/轴。独立 6×6 µm 3D 用例是更大规模的原生产品/参考对照。报告：`docs/stage9-evidence/gpu-coverage-audit/browser/gpu-resolution-live.json`；全部原生输入、输出、日志、guards、快照与对照见 `docs/stage9-evidence/high-resolution/`。

## 自动化回归

- 最终完整 GPU 回归：70 项通过、0 失败、0 错误、0 跳过。启用 `WORKBENCH_GPU_TEST=1` 后，4 个 GPU 专用 API 用例在本机 GPU 上实际执行；隔离数据目录和运行摘要见 `docs/stage9-evidence/gpu-coverage-audit/full-gpu-regression-final-summary.json`，逐测试 transcript 为同目录 `full-gpu-regression-final.log`。初次复跑中发现并修复的产品项目证据路径断言也保留在 `full-gpu-regression-summary.json` / `full-gpu-regression.log`，最终修复版通过。
- 本轮多离子限制诊断后，在隔离数据目录重跑全套 Python 回归 87 项通过、4 项 GPU 专用用例按默认配置跳过；随后显式启用宿主 GPU，原有 4 个 GPU_DISK API/重放用例 4/4 实际通过。新 transcript：`docs/stage9-evidence/gpu-coverage-audit/full-gpu-api-regression-2026-10-27.log`；Chromium 的多离子限制与 CPU_DISK 对照在 `docs/stage9-evidence/gpu-coverage-audit/browser/multiparticle-ion-limit/acceptance.json`。
- 覆盖旧格式和哈希/重放、取消与进程回收、2D/3D、连续算、几何操作、通量/中间输出、自定义材料、MultiParticle 精确 species 门控、项目偏好/重放引擎一致性、专用模型及 GPU API 运行和重放。
- TypeScript 检查和 Vite 构建通过；浏览器脚本语法检查、Python 编译检查通过。Vite 仍报告现有约 1.2 MB 的主 JS chunk 警告。
- 宿主环境的只读自检退出码为 0：许可、主机标识、安装来源与 CUDA 直通检查均通过。仅提示一个与本项目无关的本地服务端口未监听；没有启动或改动该服务。

### 候选版最终回归复核（2026-10-28）

- 新增 GPU 选择与实机读回证据断言后，隔离数据目录运行 `python -m unittest discover -s tests -v`：96 项通过，4 项默认跳过要求宿主 GPU 的 API 案例；Stage 9 专项回归 25/25。记录分别在 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/full-regression-gpu-device-evidence.log` 和 `stage9-regression-gpu-api-evidence.log`。随后用 `WORKBENCH_GPU_TEST=1` 在 RTX 3060 宿主重新执行那 4 个 GPU API 运行/历史/重放用例，4/4 通过；9 个隔离产品运行目录已归档，其中 8 个 GPU_DISK worker/重放均读回 RTX UUID、`nvidia-smi` 列出 PID，所有 bounded-child 正常退出。摘要、逐运行 worker/快照、项目、manifest、child record、日志与哈希清单在 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/gpu-api-regression-multi-device-ui/`。
- 新增真实 Chromium 的合成双设备清单测试：将物理 RTX 3060 和仅用于控件测试的第二 UUID 一同显示，选择第二项并保存、重开、导出、再导入，UUID 全程保留；测试明确不提交合成设备仿真。报告为 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser/synthetic-multi-device-ui.json`，outer bounded-child guard 为 `synthetic-multi-device-ui.runner.guard.json`。本机只有一张物理 CUDA 卡，故真实第二张卡的跨卡仿真仍未验证；这项测试证明设备选择控件和项目持久化路径，不冒充硬件实测。
- 新增真实 Chromium 路径保持 `enginePreference=auto` 且未提供设备 UUID；实际运行记录选择 RTX 3060 GPU_DISK。显式 UUID 保存重开、项目运行、导出重放和设备 UUID 固定均通过，产品快照与直接 ViennaPS 完全一致。报告：`docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/browser/acceptance.json`，浏览器外层 bounded-child guard 为 `browser-runner-final.guard.json`。
- 新增自定义材料 GPU 网页验收：真实 Chromium 分别在 2D/3D 注册自定义材料、输入材料速率、选定 RTX 3060并运行平面单粒子刻蚀；实际产品 worker 读回 CUDA UUID，`nvidia-smi` 列出 worker PID。项目保存、重开、导出和重放保留各自的原生材料 ID（本次 ID 187/188）；初始、最终和重放网格均与独立 ViennaPS 直调逐项一致。测试还覆盖 2D→3D 转换的 16 项可见材料上限，并确认自定义基底仍可见。报告：`docs/stage9-evidence/gpu-custom-material/browser/acceptance.json`；产品、重放、直接参考、快照及 guard 在同目录。`gpu-custom-material/summary.json` 只有在直接测量和上述网页证据都通过时才把 `exposedToWebUI` 设为 true，自动回归重新校验项目引擎选择。
- 这项网页验收期间发现 2D→3D 转换会把全部材料目录复制到最多 16 项的 `visibleMaterialIds`，新增自定义材料后产生 17 项、保存时被 schema 正确拒绝。现在 3D 新建、维度转换和 3D 工程中注册材料都会优先保留几何实际使用材料，并将可见集合限制在 16 项内。
- 随后增加了项目导出 JSON 与再导入的真实浏览器检查：导出文件保留选定 UUID，重新导入后 GPU 下拉框恢复相同设备；证据见上述报告的 `projectExportImport`、`gpu-device-exported-project.json` 和 `export-import-runner.guard.json`。
- 首次设备选择浏览器脚本曾因旧断言检查 `<option>` 显示文本是否含 UUID 而失败（UUID 实际位于 option value）；该测试断言已改为检查真实 value，后续自动选择、显式 UUID、运行/重放和导入导出全部通过。原始诊断保留为 `acceptance-initial-failure-historical.json`，不代表当前验收结果。
- 针对先前报告的几何预设 GPU 不可用，真实 Chromium 在显式选择同一 RTX 3060 后分别运行 2D SF₆/O₂ 平面、2D 单粒子沟槽、3D 单粒子圆孔和 3D Boolean Fin；四项均实际进入 GPU_DISK，worker PID 被 `nvidia-smi` 归属到该卡，初末坐标、拓扑、材料 ID 和 level-set 映射均与对应独立 ViennaPS 输出完全一致。页面无错误。自动脚本 `frontend/tests/stage9-gpu-presets-live.mjs`，报告与每项输入、worker 结果、child guard、日志见 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/presets-browser/`。
- 性能验收的真实几何负载为 3D 平面刻蚀。五组 20 线程配对测量 GPU `Process.apply` 中位 1.571 s、CPU 4.513 s，中位加速 2.873×，95% bootstrap 区间 2.446–3.020×。各 GPU 样本均读回同一 RTX 3060 UUID，产品与各自独立 ViennaPS 结果一致；CPU/GPU 最终三角网格不完全相同，但材料映射一致，最大对称最近顶点距离为 0.020003 µm，小于 0.04 µm 网格间距。报告 `docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/performance/benchmark-report.json`。
- 回归曾发现 worker 扫描 artifacts 时与 supervisor 原子替换 `manifest.json` 竞争，修复为忽略临时 JSON 且容忍扫描期间文件消失；竞态测试包含在当前 96 项套件中。
- 正式 8769/8789 服务未被本轮操作触碰。隔离候选 8803 使用数据目录 `var/browser-gpu-export-import-acceptance-2026-10-28`，由项目 `scripts/start.sh` 启动并通过 `scripts/status.sh` 确认健康；端到端浏览器运行后仍保持运行，便于查看本轮候选界面。项目默认 `var` 目录中发现的既有候选实例 8767 未操作。

## 限制和未开放组合

- 安装版 `FluxEngineType` 列有 `GPU_TRIANGLE` 和 `GPU_LINE`。新增 GPU_TRIANGLE 诊断用 Stage 8 产品 worker/独立 ViennaPS 直调分别执行 2D、3D SF₆/O₂；两例均读回 RTX 3060 UUID、`nvidia-smi` 识别 worker PID，初末坐标、拓扑、材料数组和材料映射一致。该证据已进入 schema-7 能力矩阵（2 条 `GPU_TRIANGLE` 行：`specialized:sf6o2` / etch / reflective / stack / pattern=none，2D 与 3D），通过 `solver.gpuEngine=GPU_TRIANGLE` 显式选择；其它模型、维度、边界或几何一律 422，不回退到 GPU_DISK/CPU_DISK。GPU_LINE 的独立 ViennaPS 直调在 2D 和 3D SF₆/O₂ 上均成功并改变点坐标；这些调用没有记录 CUDA UUID，也没有产品 worker/产品-直调对照或 WebUI 保存/重放验收，因此 GPU_LINE 仍不暴露。证据：`docs/stage9-evidence/gpu-variants/gpu-triangle-{2d,3d}/summary-gpu_triangle.json` 和 `gpu-line-{2d,3d}-direct.{json,guard.json,log}`。
- 通用 SingleParticle 按矩阵实测签名开放；能力矩阵中的材料范围才是开放边界。自定义原生材料 ID 177–240 的 GPU 直调和多种 GDS/材料角色已按矩阵证据覆盖；另有网页端原生 ID 187/188 的 2D/3D 反射边界平面基底案例完成注册、GPU 运行、保存、重开、导出及重放，产品初末和重放网格与直接 ViennaPS 完全一致。网页证据和自动检查见 `docs/stage9-evidence/gpu-custom-material/summary.json` 与其 `browser/` 子目录；这不扩大到矩阵未覆盖的模型或几何组合。
- 本阶段 specialized 和 MultiParticle 的 GPU 边界能力按精确矩阵开放；此前 8 个 periodic GDS 崩溃组合已通过隐藏周期支撑图形修复，并经 2D/3D GPU_DISK 与 CPU_DISK 产品/直接对照及真实浏览器运行验证。超出周期 Domain 的 GDS 布局仍需扩大 Domain 或调整放置。通用旧项目的 GPU 组合只沿用各自原有兼容能力证据。
- 安装版 ViennaPS 的 GPU MultiParticle 绑定只转换一种离子类型，多个离子类型会被忽略。单次通量 CPU/GPU 对照已确认 GPU 缺少第二离子通道；运行 API 和 UI 显示该限制、禁用受影响的 GPU 请求，而相同配置的 CPU_DISK 已在 8 个 2D/3D 产品/直接对照中验证。进一步的 bounded Python API 探测发现 `ProcessModel.setVelocityField` 需要未导出的 `viennaps::VelocityField<double, D>`，读取该对象不能转换为 Python，ViennaLS 基类和 Python 子类也都会被拒绝；因此当前安装接口没有可用的安全 Python 合并通量路径。非 `FluxEngineType` 步骤（例如定向/各向同性和几何操作）标记为 `not-applicable`，不会伪报 GPU_DISK。
- Stage 8 的 `OxideRegrowth` 在带图案结构上会触发 ViennaPS 4.6.2 原生崩溃；UI 只允许已验证的平坦 SiO2/Si3N4 结构。未声明单位的原生参数仍明确标记为未声明。
- 运行时不再限制网格间距下界、网格间隔数、表面点数、三角形数或单快照 JSON 大小。2D/3D 分辨率实测见 `docs/stage9-evidence/gpu-coverage-audit/README.md`。这仍不是无限资源：bounded child 的超时、进程 RSS 轮询、取消/回收、日志和单次产物限制及工作台存储配额继续生效；RSS 轮询不测 CUDA 显存，物理域范围和几何特征有效性检查也继续生效。
- 前端构建有非阻断的大 chunk 警告。默认全量 Python 回归会跳过 4 个要求宿主 GPU 的 API 用例；这些路径曾另行在本机 GPU 权限下运行通过，且设备选择、性能、自定义材料及几何预设另有当前真实 Chromium/CUDA 证据。GPU_KERNEL/引擎枚举不等于 WebUI 已支持。
