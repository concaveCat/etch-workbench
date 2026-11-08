# viewer3d 基准结果

`frontend/src/workflow/viewer3d/` 的 3D/2D 工艺结构查看器基准。数据由
`scripts/run-viewer3d-benchmark.sh` 生成，机器可读版本见
[`docs/viewer3d-benchmark.json`](./viewer3d-benchmark.json)。

## 环境

| 项 | 值 |
|---|---|
| CPU | 12th Gen Intel Core i7-12700H（20 线程） |
| 内存 | 11 GB |
| 浏览器 | 沙箱/无头：Playwright Chromium 1.56.1，headless；真机：Windows Edge（CDP） |
| WebGL 后端 | 沙箱/无头：ANGLE / Vulkan / **SwiftShader**（没有 `/dev/dri`，无硬件 GL）；真机：**NVIDIA GeForce RTX 3060 Laptop GPU**（D3D11） |
| 画布 | 1254 × 594（1280 × 800 视口，devicePixelRatio = 1） |
| 负载 | 采集时 1 分钟负载 5–9（共享主机，其他 agent 同时在跑任务） |

## 场景

harness 在页面里生成合成快照，不依赖后端：

- `step-1` / `step-2`：**239,432 三角形**、5 种材料、起伏表面（不同相位/种子）；
- `step-3`：**700,928 三角形**，用来验证顶点聚类简化；
- `?dimension=2`：720 点、719 段的 2D 剖面，3 种材料。

## 结果（沙箱/无头软件回退，最近一次通过）

> 下表是 **SwiftShader 软件光栅化**的结果，只用于 CI 回归和“查看器本身没有退化”的判断，
> **不代表真机性能**。硬件实测见下一节。

| 指标 | 结果 | 门槛 |
|---|---|---|
| 三角形数 | 239,432 | ≥ 200,000 ✅ |
| 旋转 120 帧平均帧时间 | **148.2 ms**（p50 172.6 ms，p95 226.1 ms） | ≤ 33 ms ❌（软件） |
| 同机裸 WebGL2 基线（同分辨率、同三角形数、最简着色器） | **50.3 ms/帧** | — |
| 首次切换步骤（冷解析） | **191.7 ms** | < 800 ms ✅ |
| 缓存命中切换步骤 | **29.6 ms** | < 150 ms ✅ |
| 70 万三角形步骤（聚类简化） | 1,164 ms，700,928 → 188,298 面 | ≤ 300k ✅ |
| 走 API（worker fetch）的步骤 + 空闲预取后的切换 | 29,768 面，**4.9 ms**（parseMs = 0） | < 150 ms ✅ |
| 2D 画布 | 9,913 个非背景像素，3 种材料色 | ✅ |
| console error | 0 | ✅ |

多次运行（同一构建，主机负载 5–9）的区间：查看器平均帧时间 138–228 ms，裸 WebGL2 基线
50–93 ms，查看器/基线比值稳定在 **1.9–3.3×**。数字随主机负载浮动，仓库里的
`docs/viewer3d-benchmark.json` 是最新一次完整运行。

同一次运行里的其它断言（都通过）：画布有像素且 5 种材料色都出现、裁剪面缩小可见 X 跨度并
重新取景、线框/表面切换改变图像签名、隐藏 Si 后琥珀色像素基本清零、renderer 字符串默认隐藏
（`性能` / `?debug=1` 才显示）、软件渲染会在面板给出提示。


## 硬件实测（RTX 3060 Laptop / Windows Edge）

同一份 harness、同一份构建，用 Windows 上的 Edge 打开（`--force_high_performance_gpu`），
再通过 CDP 让 benchmark 直接连到真实浏览器（`VIEWER3D_CDP_URL`）：

| 项 | 值 |
|---|---|
| WebGL renderer | `ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Laptop GPU (0x00002560) Direct3D11 vs_5_0 ps_5_0, D3D11)` |
| 239,432 三角形，旋转 120 帧 | **16.67 ms/帧（59.97 FPS）**，p95 **17 ms** |
| 全细节静态帧 | **16.66 ms** |
| 首次切换步骤（冷解析） | **55 ms** |
| 缓存命中切换步骤 | **8.4 ms** |
| 70 万三角形步骤（顶点聚类） | 700,928 → **188,298** 面，**531.7 ms** |
| 33 ms 帧预算 | ✅ 通过（约为预算的一半） |

结论：真机上查看器稳定跑满 60 FPS（vsync 上限），之前的 175–194 ms/帧完全来自沙箱的
SwiftShader 软件回退。机器可读版本见 `docs/viewer3d-benchmark.json` 的 `hardware` 段。

> Windows 可能把 Edge 默认分给核显。要强制使用 RTX，请在
> **设置 → 系统 → 显示 → 图形 → 添加桌面应用 → Microsoft Edge → 高性能**，
> 或在启动参数里加 `--force_high_performance_gpu`；随后在 `edge://gpu` 确认
> renderer 是 `NVIDIA GeForce RTX 3060 Laptop GPU` 而不是 `Intel(R) Iris Xe`。

### 关于 33 ms 门槛

**在无头 Chromium 下达不到（真机 RTX 3060 Laptop 为 16.67 ms，已通过），原因是软件光栅化，不是查看器的 draw call。**

- 同一次运行里，**裸 WebGL2**（一个 `drawElements`、一个位置属性、导数法线 + 一盏方向光、
  同样 239,432 个三角形、同样画布）需要 **92.6 ms/帧** —— 已经超过 33 ms 两倍。
- 查看器约为基线的 2–4×（随主机负载）。差距来自 vtk.js 的完整片元着色（光照、
  裁剪面、颜色查找表）以及朝向坐标轴的第二遍渲染；单独关掉朝向坐标轴只快约 5–10%。
- 硬件 GPU 上这个场景是 16.67 ms/帧（RTX 3060 Laptop，见上一节）；沙箱 headless 只能走
  SwiftShader，所以 175–194 ms 反映的是软件光栅器，而不是真实用户环境。
- 负载对数字影响很大：同一份构建在主机空闲时测得 95 ms（等轴视角、同步回读），
  在负载 8–9 时测得 200 ms 以上。

已经做过的优化（见模块 README）：

1. **单 actor / 单 draw call**：全部材料共用一个 `vtkPolyData` + 查找表，而不是每种材料
   一个 actor。
2. **索引化顶点缓冲**：vtk.js 只要发现 *cell* 标量就会摊平成每三角形 3 个顶点
   （`CellArrayBufferObject.canUseIndexedVBO`）。这里在 worker 里按材料拆分顶点、改用
   *point* 标量，顶点数从 3×三角形（约 72 万）降到约 12 万，同时材料边界依然锐利。
3. **超过 50 万三角形自动聚类简化**到 ≤ 30 万（O(n) 网格 snap，保留 materialId），
   避免 70 万面的步骤卡死。
4. **解析全在 worker**（fetch + JSON.parse + 简化 + 拆分），主线程只做 GPU 上传。
5. **LRU(8) + 空闲预取**：相邻步骤切换 11 ms。

**没有采用的取舍**：

- 没有把静态显示也降到 33 ms 所需的约 4 万三角形——那会让材料边界和形貌严重失真，
  属于用画质换数字。
- 没有默认打开背面剔除（可再快约 2×）：ViennaPS 的界面网格可能是开放曲面，从背面看会
  直接消失。需要时可以在 `vtkScene.ts` 里给 `actor.getProperty()` 打开
  `setBackfaceCulling(true)`。

## 复现

### 沙箱 / CI（SwiftShader，默认）

```bash
cd /root/wb-workflow-p0
scripts/run-viewer3d-benchmark.sh
```

脚本会：构建 harness 到 `$TMPDIR/viewer3d-bench-dist` → 用 `vite preview` 起静态服务
（默认 8811，被占用时自动顺延到下一个空闲端口，避开 8769/8789/8819/8820）→ 跑
`frontend/tests/viewer3d-benchmark.mjs` → 把 JSON 写到 `docs/viewer3d-benchmark.json`，
并在日志里打印 `renderer` 与 `environment`（`software` / `hardware`）。

### 真机 GPU（Windows Edge + CDP）

```powershell
# Windows：以调试端口启动 Edge，并强制高性能 GPU
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
  --remote-debugging-port=9222 --force_high_performance_gpu ^
  --user-data-dir=%TEMP%\edge-bench
```

```bash
# 同机（或能访问该端口的 WSL/容器）：
VIEWER3D_CDP_URL=http://127.0.0.1:9222 scripts/run-viewer3d-benchmark.sh
```

设置 `VIEWER3D_CDP_URL` 后 benchmark 用 `chromium.connectOverCDP(url)` 复用已开着的
Edge，不再启动本地 headless；结束时只关闭自己打开的页面，不会关掉用户的浏览器窗口。
报告里的 `webglRenderer` / `environment` 会记录实际后端。若 Windows 把 Edge 分给核显，
按上一节把 Edge 设为“高性能”后重跑。

单独调试 harness：

```bash
cd frontend
VIEWER3D_BENCH_OUT=/tmp/v3d npx vite build --config tests/vite.viewer3d.config.ts
VIEWER3D_BENCH_OUT=/tmp/v3d npx vite preview --config tests/vite.viewer3d.config.ts --port 8811
# http://127.0.0.1:8811/viewer3d-harness.html?tris=240000
# http://127.0.0.1:8811/viewer3d-harness.html?dimension=2
```
