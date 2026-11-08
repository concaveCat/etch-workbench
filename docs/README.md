# 工作台文档入口

当前正式发布与启动说明见项目根目录 `README.md`。阶段 1–9 的实现范围、安装版 ViennaPS 原生调用、参考对照、自动化及浏览器验收见对应的 `ACCEPTANCE_STAGE*.md`。

- `ACCEPTANCE_STAGE9.md`：GPU_DISK 精确能力矩阵、分辨率扩展和整体回归。
- `native-capability-release.md`：GPU_TRIANGLE、任意轴 Slice、HoleShape HALF/QUARTER 与 halfFin/halfTrench 的 schema、证据范围、原生调用与拒绝规则。
- `stage9-evidence/gpu-capability-matrix.json`：逐模型签名与产品/direct-reference/guard 证据索引。
- `cpu-thread-evidence/`：本机 CPU 动态上限、schema 7 默认多粒子 GPU_DISK 浏览器运行/重放及发布页 smoke 证据。
- `schema-project-union.json`、`schema-project-v*.json`、`schema-replay-v*.json`：项目和重放格式；旧格式兼容规则在 API contract 与阶段报告中。
- `cmp-planarize-contract.md`：材质感知平坦化（按材料停止的 CMP）与每步实测去除/增加厚度。
- `gds-boolean-contract.md`：schema 7 三维 GDS 工程的 `gdsBoolean` 步骤（版图图层作为原生布尔操作数）的坐标、语义与限制。
- `plasma-advanced-parameters-contract.md`：四个内置等离子体模型的 `nativeParameters.advanced` 严格子参数契约、逐模型字段与原生构造路径。
- `particle-shape-distribution-contract.md`：schema 7 `particleShape`（原生 Box/Sphere 几何分布）的探测证据、掩膜刻蚀可用 / 掩膜沉积必崩的原因、参数边界与 `CustomSphereDistribution` 阻断记录。
- `engine-capability-matrix.md` / `.json`：安装版 ViennaPS 4.6.2 的公开类/参数面与工作台暴露面的逐类对照、SPX-only 能力清单，以及按可验证批次拆分（含验收标准）的剩余能力计划。
- `engine-complete-20261030.md` / `engine-checkpoints-20261030.md`：引擎批次 A1–A3、B1–B5 的完成记录、独立复查、未暴露清单与存档 tag。
- `oxidation-advanced-contract.md`、`ald-surface-diffusion-contract.md`、`csv-process-contract.md`：B5、B3、A3 的严格 schema 与原生插入点合同。
- `finfet22-geometry-m0.md`：22 nm FinFET 的 geometry-only wafer→M0 run、replay 与外部存档记录。
- `v2-session-dataflow.md`：worker 的 run 级 v2 session、每步 replay 布局与 registry/provider 复用边界。
- `engine-plugins.md`：engine.toml / engines.lock / provider 注入、ViennaPS 与自定义引擎的同一入口，以及 SurfaceBackend。
- `implementation-status.md`：早期基线历史快照，不代表当前服务端口或当前验收状态。
