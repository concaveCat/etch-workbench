# 第1阶段：DirectionalProcess 验收记录

状态：候选实现、自测和真实 Chromium 端到端验收通过。没有替换正式目录 `/root/viennaps-workbench-selectivity`，也没有接管 8769。

## 实现

- schema 5 定向步骤和控件：方向、定向速率、各向同性分量、遮挡开关、刻蚀/沉积、沉积材料。二维/三维方向映射在界面、验证与历史参数中明确。
- 使用本机 ViennaPS 原生 `DirectionalProcess`，并按操作设置原生速度符号。传递掩膜材料列表；全零步骤作为 no-op；定向速率为零时遮挡不适用。
- 历史保存实际单位方向、原生有符号速度、可见性、掩膜名/ID，以及射线参数不适用标记。
- 定向步骤限定为已验证 CPU 路径；schema 5 请求 GPU 时后端拒绝，不会静默转用 CPU。
- schema 5 同时容纳 schema 4 工艺步骤；项目保存/打开、导出 ZIP、历史查看和 replay project 支持该格式。旧格式 1–4 的哈希实现保持不变。
- 实施前检查 `/tmp/viennaps-complete-build` 与正式基线的源码差异：排除数据、构建和缓存目录后，只有三份草稿文档不同；本实现从正式基线单独复制，不采纳草稿代码。

## 验证

- 本机版本探测：ViennaPS 4.6.2、ViennaLS 5.8.5。原生符号探测覆盖二维/三维平面生长/回蚀，以及沟槽可见性开关和空/非空掩膜列表；结果见 `directional-evidence/native-sign-probe.json`。
- `tests.test_directional` 通过真实 FastAPI ASGI 路径提交任务，随后由受限工作进程调用 ViennaPS；独立参考脚本也经 `scripts/bounded_child.py` 直接调用 ViennaPS。2D 刻蚀、3D W 沉积、2D/3D 遮挡开/关和保存后重放均比较了坐标、拓扑、材料数组及材料映射。比较误差为 0，坐标容差为 1e-10。
- 真实沟槽输出显示可见性开关会改变输出：2D 点数 49/50，3D 点数 1029/1050（开/关）。全零速率重放为 no-op。
- 本机 ASGI 路径验证保存、重新打开、验证、运行结果 ZIP、历史原生参数和 replay；结果证据按测试标签保存在 `directional-evidence/`。
- 前端 TypeScript 检查和 Vite 生产构建通过。`frontend/tests/directional-controls.mjs` 是离线 mock 控件脚本，不作为真实运行证据。
- 完整 Python 回归 36 项通过，1 项宿主 GPU_DISK 测试因当前能力未验证而跳过；回归日志在 `directional-evidence/regression.log`。
- 历史的 2D Chromium 验收记录了 run `b632ab15628a937d4983a064` 与 replay `a739ef80f540430f67061d37`；证据见 `directional-evidence/browser-live.json`。其中的 8779 URL 只描述该轮测试目标，不代表当前服务状态。
- 为补齐真实浏览器 3D 覆盖，新增 `frontend/tests/directional-3d-live.mjs`，在隔离候选端口 8786 实际提交 [0.6, 0, -0.8] 方向、0.25/0.05 µm/s 定向/各向同性分量和 W 沉积材料。ViennaPS 3D 网格的 882 点、1600 个三角面、材料数组和材料映射与独立 `directional_direct_reference.py` 完全相同；bounded guard 返回码 0、无残留子进程。重建最新前端后复跑，真实 UI 的历史、保存、重开、导出和重放均通过；run `fc51e92d49c5a6c9170473b1`，replay `99a3e876841bd54f45da587d`。记录见 `directional-evidence/browser-3d-live.json`，提交项目、direct 输出、guard 和日志均在同目录。
- 真实浏览器调试发现 `snapshotDimension()` 未接受 schema 5 快照，导致结果网格和历史原生参数不显示；已补齐 schema 5 维度识别并重建。修复后真实历史页面显示方向和有符号速度。

## 候选服务状态说明

旧 2D 浏览器记录的 URL 不再用作候选服务管理入口。后续现场检查统一使用隔离候选端口和项目生命周期脚本；正式 8769 未触碰。

详细参数和格式合同见 `directional-contract.md`。回归日志和命令见 `directional-evidence/regression.log`。
