# 三维交付验收台账

独立候选：`/root/viennaps-workbench-3d`。二维基线：`/root/viennaps-workbench`。当前候选端口身份记录在 `var/server.json`；候选数据目录独立。源码、engine 版本和首批证据见 `var/evidence/baseline-checksums.json`、`g1-record.json`、`g1-r2-record.json`。G1 原生探测全程由 bounded_child.py 启动、限时/RSS/日志并回收进程组。

| 项 | 当前状态 | 证据 / 限制 |
|---|---|---|
| T01 | 通过（代表性 fixture 与二维回归） | `tests/fixtures/legacy/index.json`、`var/evidence/t01-legacy.json`；含旧 v1 hash、项目、replay、JSON、VTK 哈希；二维后端/worker/guard/zip 回归在 `backend-final-tests.log`。迁移后再次核对待 T18。 |
| T02 | 通过 | 非对称 x=6、y=4；两种边界、间距及三轴读回见 `var/evidence/g1/results.json`、`t02-t05.json`。周期活动带节点范围保留为独立读回字段。 |
| T03 | 通过 | plane、trench、FULL hole、掩膜、0/±10°真实几何、两种边界，`t02-t05.json`；离散几何容差 2Δ。 |
| T04 | 通过 | `tests/test_3d.py`、`backend/snapshot_models.py`；triangle connectivity、材料单元顺序、原始 VTK 独立读取和 1e-4 µm JSON/VTK 坐标容差。 |
| T05 | 通过 | 两个边界、isotropic 刻蚀/沉积的 z 位移 `t02-t05.json`，误差不超过 2Δ。 |
| T06 | 通过 | CPU_DISK 粒子圆孔沉积→刻蚀与独立原生调用逐点距离为 0，材料/拓扑一致，`t06-t08-t14.json`。 |
| T07 | 通过（合成快照语义与哈希顺序） | 两次同材料沉积无虚构内部面；零速率快照不新增层；启用步骤顺序影响 requestedInputHash、禁用步骤不影响，`t07-order-hash.json`、`t07-t13.json`。 |
| T08 | 通过（CPU_DISK 与 GPU_DISK） | CPU 固定种子 replay 顶点距离为 0；宿主 GPU 重复运行对应点最大距离为 0；浏览器导入并重放 GPU 固定种子运行通过。证据：`var/evidence/gpu-host/summary.json`、`var/evidence/browser-3d.json`。 |
| T09 | 通过（4 个已验证 GPU_DISK 组合） | 宿主 NVIDIA GeForce RTX 3060 Laptop GPU 实测 `singleParticle` 的刻蚀/沉积 × reflective/periodic，均有三角网格且固定种子重复距离为 0；没有 GPU fallback。能力表：`docs/capabilities-3d.json`；逐组合结果：`var/evidence/gpu-host/summary.json`。另有 2 条已归档的 `GPU_TRIANGLE` 产品/direct 行（2D/3D SF₆/O₂ 刻蚀，reflective stack），仅在 schema-7 `solver.gpuEngine=GPU_TRIANGLE` 下开放；未覆盖组合 422。GPU_LINE 仍未探测，不对三维暴露。 |
| T10 | 通过（最终代码真实浏览器验收） | 真实旋转/平移/缩放、三向标准视图、正交/透视、材料/透明度/线框和 PNG 均通过；最终报告 `var/evidence/browser-3d.json`，视图截图 `var/evidence/browser/view.png`。 |
| T11 | 通过（最终代码真实浏览器验收） | x/y/z 双侧裁剪及真实等比例截面通过，证据 `var/evidence/browser-3d.json`、`var/evidence/browser/section-detail.png`；原生孔/沟槽中心开口和深度读回见 `t11-sections.json`。 |
| T12 | 通过（最终代码真实浏览器验收） | 三维比较共享 camera/clipping，二维历史由 v1 SVG 查看器打开，跨维度叠加禁用；证据 `var/evidence/browser-3d.json`、`var/evidence/browser/comparison-section.png`、`var/evidence/browser/legacy-routing.png`。 |
| T13 | 项目持久化、刷新/重开和维度草稿通过；服务重启待 T18 | 浏览器保存/刷新/重开并保留三维 view3D camera 通过；维度切换另建草稿且来源项目读回完全相等。部署服务停止/重启与回滚仍属于待执行的 T18。 |
| T14 | ZIP/VTK 下载通过 | `browser-download-verified.json`、`t06-t08-t14.json`；下载包的 project/replay/manifest/log/真实 VTK/snapshot 独立读回，回放前后网格与材料数组精确一致。 |
| T15 | 通过 | 真实原生 worker 注入低点数、面数、JSON 阈值，均失败而无伪快照；三维 cancellation/预算和采样在 `t15-t16.json`。 |
| T16 | 通过 | 运行中 health/status/cancel 合计 19 ms；取消到回收结束 0.41 s、没有孤儿，`t15-t16.json`。 |
| T17 | 通过（最终代码真实浏览器闭环，含 GPU 和新维度入口） | `var/evidence/browser-3d.json` 状态 passed、consoleErrors 为空，共 16 项检查；包括“新建工程”、2D/3D 切换、圆孔转沟槽、来源工程保留、CPU/GPU replay、真实 ZIP、PNG、旧二维路由。24 次大快照切换后 JS 堆增长 46,640 bytes。截图在 `var/evidence/browser/`。 |
| T18 | 尚未完成 | 计划显示旧服务12个项目/147条运行；数据复制清单见 `migration-plan.json`。尚未复制/切换/重启/回滚，按用户“尚未准备好”保留 8767 候选。最终命令为 `./.venv/bin/python scripts/finalize-host.py`，包含逐文件 SHA、restart、rollback 与 8766 最终浏览器 smoke。 |

已测试：`backend-final-tests.log` 为 3D workflow + 原有 API integration；一项二维 GPU host-only 测试因明确 opt-in 而 skip。`regression-r3.log` 覆盖 bounded-child、zip、legacy VTK 和 schema 回归。资源限值/设备证据见 `docs/resource-policy-3d.json` 和三维能力文件。

当前不要将 8767 候选报告为正式切换完成；按用户指示，T18 的迁移、停服/重启、回滚和正式入口 8766 验收仍待用户准备好后执行。宿主已验证的四个三维 GPU_DISK 组合已开放给对应 UI 请求；GPU_TRIANGLE 仅对已归档的 2D/3D specialized sf6o2 刻蚀 stack 组合开放（显式 `solver.gpuEngine`），GPU_LINE 仍因未探测而不暴露。sandbox cuInit 失败仅代表 sandbox 不能调用 GPU，不代表宿主 GPU 不可用。宿主验收输出是 GPU 能力的权威证据；当前 Codex 沙箱无法观察宿主进程与本机端口。

本轮前端构建记录在 `var/evidence/frontend-build-dimension-switch.log`；浏览器脚本通过 `node --check frontend/tests/browser-3d.mjs`。更新后的宿主验收于 `2026-10-26T11:44:46Z` 执行，浏览器报告 `passed`，GPU 汇总 `verified-combinations`、失败数 0。后续 UI 改动再通过 `./scripts/verify-host-capabilities.sh` 更新能力与完整浏览器证据。
