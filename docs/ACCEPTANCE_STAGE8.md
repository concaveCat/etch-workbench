# 第 8 阶段验收：ViennaPS 4.6.2 专用工艺模型

日期：2026-10-27  
运行环境：ViennaPS 4.6.2、ViennaLS 5.8.5  
候选服务：`http://127.0.0.1:8784/`，数据目录 `var/stage8-browser-data`。正式服务 `8769` 未修改。

## 实现

- 在统一 schema 7 中接入 14 个安装版原生模型：SF₆/O₂、CF₄/O₂、HBr/O₂、SF₆/C₄F₈、Fluorocarbon、IBE、WetEtching、TEOSDeposition、TEOSPECVD、SingleParticleALD、Oxidation、OxideRegrowth、SelectiveEpitaxy 和 NeutralTransport。项目格式 1–6 保持兼容。
- 网页从安装版模型目录生成专用参数控件；参数通过后端 allowlist 构造器逐项传给对应 ViennaPS 类。专用模型不叠加通用工艺速率；有物理持续时间的模型保留秒输入，Oxidation 使用原生小时参数。
- 验证按模型检查枚举、数值、材料表、沉积材料、掩膜以及几何前置条件。可选空掩膜不会被错误解析成材料名称。历史记录保留原生模型、参数、实际引擎、随机种子及线程信息。
- 通量模型固定使用 `CPU_DISK`；湿刻蚀、热氧化、氧化再生和选择性外延不使用射线引擎，历史中记为 `not-applicable`。当前只开放反射边界下通过对照的组合，GPU 和周期边界仍关闭。
- `Process.calculateFlux()` 已接入单次通量计算，原生点/单元字段、拓扑和网格身份随结果保存和导出，不映射到另一张网格。

## 原生执行和直接对照

- 14 个模型各自在 2D、3D 执行一次，共 28 组产品 worker 运行。每组均由 `scripts/bounded_child.py` 启动，直接参考独立构造原生模型，不导入产品模型构造器。
- 28 组产品结果和直接 ViennaPS 参考在初始及最终表面网格上，坐标、拓扑、材料数组和 level-set 材料映射逐项相等。OxideRegrowth 另逐项比对 DenseCellSet 的单元数和字段。
- 真 Chromium 网页测试对每个模型在 2D、3D 提交非默认参数，检查提交 JSON、原生执行历史及结果查看器。28/28 运行成功，网页结果与各自独立直接参考完全一致，无页面错误或控制台错误。
- SF₆/O₂ 网页流程通过项目保存、重开、JSON 导出、运行 ZIP 导出、重放及单次通量计算。运行 ZIP 含 `project.json`、`replay-project.json` 和初始/工艺完成时的 `.vpsd` 状态。单次通量的坐标、拓扑、字段关联、字段值、网格身份、射线元数据与直接 `Process.calculateFlux()` 结果相等。

## 证据

- 全模型产品/直接调用矩阵：[`product-worker/summary.json`](stage8-evidence/product-worker/summary.json)；逐模型输入、快照、原生日志、bounded guard 和直接参考均在同目录。
- 反射边界 CPU 能力白名单：[`capability-matrix.json`](stage8-evidence/capability-matrix.json)。通量模型标记 `CPU_DISK`，非射线模型标记 `not-applicable`。
- 全模型真实网页验收：[`browser-live.json`](stage8-evidence/browser/browser-live.json)；逐用例项目、直接参考、日志和 guard 在 `stage8-evidence/browser/{2d,3d}-<model>/`。
- 嵌套原生参数与安装版绑定清单：[`binding-inventory/`](stage8-evidence/binding-inventory/)。
- 全量 Python 回归：[`full-python-regression.json`](stage8-evidence/full-python-regression.json)，57 项通过，1 项因 GPU 宿主探测尚未运行而跳过。
- `scripts/build-frontend.sh` 通过 TypeScript 检查和 Vite 生产构建。Vite 保留已有的大 bundle 提示。

## 限制与主代理复核点

- **OxideRegrowth 几何限制：**本机绑定在带沟槽/圆孔的 stack 上进入 ViennaPS 后会段错误（bounded guard 记录 `returnCode: -11`）；相同输入由独立原生参考也复现。平坦、无顶层图案且含 SiO₂/Si₃N₄ 的 stack 在 2D、3D 均通过。服务端现在拒绝其他几何，界面说明条件并提供将顶层图案改为“无”的按钮。复现材料在 `stage8-evidence/debug-oxide-regrowth/`，产品进程记录在 `var/stage8-browser-data/runs/f685d668eacaa92cf1049051/`。
- 安装版绑定对多数专用模型参数没有声明单位。工作台按原值传递并显示“单位未声明”；只对 ViennaPS 原生接口明确记录的 Oxidation 字段显示 °C、atm、小时和 µm/hr。主代理可复核参数标签与本机绑定库存。
- 本阶段没有启用 GPU 或周期边界。后续第 9 阶段会做宿主 GPU 探测；若某个组合未完成产品级显式 GPU 验收，仍保持禁用且不会回退到 CPU。

第 8 阶段完成；继续第 9 阶段验收。
