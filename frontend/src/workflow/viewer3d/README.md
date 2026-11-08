# viewer3d —— 工艺结构快照查看器

`WorkflowViewer3D` 用来逐步预览一个 run 的 snapshot：3D 用 vtk.js 渲染，2D 用
canvas 直接画原生界面线。整个模块自包含，不依赖 `WorkflowApp.tsx` 或任何现有组件，
样式全部在 `viewer3d.css` 里（`.wb-viewer3d` 命名空间）。

```
frontend/src/workflow/viewer3d/
  WorkflowViewer3D.tsx      对外组件（加载状态机 + 工具栏 + 图例 + 状态栏）
  Viewer2D.tsx              2D canvas 线框渲染器（坐标轴、刻度、比例尺）
  vtkScene.ts               vtk.js 场景管理（单 actor、LUT、裁剪、相机、rAF 合帧）
  snapshotCache.ts          worker 调度 + LRU(8) + 空闲预取
  snapshotParse.worker.ts   解析/简化 worker（fetch + JSON.parse 都在这里）
  colors.ts                 材料颜色解析（快照 → 材料目录 → 调色板 → 灰色）
  types.ts                  SnapshotDoc / ParsedMesh / props / handle
  viewer3d.css              独立样式
```

## 父组件接入

```tsx
import WorkflowViewer3D from './viewer3d/WorkflowViewer3D';

<WorkflowViewer3D
  runId={runId}
  stepId={selectedStepId}
  dimension={run.dimension === 3 ? 3 : 2}
  height={460}
  marker={{ axis: 'x', value: recipeX, label: '测量 X 0.2 µm' }}
  onStepUnavailable={(reason) => setViewerNotice(reason)}
  onStats={(stats) => console.debug(stats)}
/>
```

### 1. 取 run manifest

`GET /api/runs/{runId}` 返回 `{ snapshots: [{ stepId, name, stepIndex, ... }], ... }`。
父组件只需要把 `stepId` 列表按 `stepIndex` 排序后交给查看器（可选）：

```tsx
<WorkflowViewer3D runId={runId} stepId={stepId} dimension={3} stepIds={orderedStepIds} />
```

不传 `stepIds` 时，查看器会自己拉一次 manifest 并缓存（仅用于空闲预取相邻步骤）。

### 2. 取 snapshot

**推荐：不要自己取。** 不传 `snapshot` 时，`snapshotParse.worker.ts` 直接在 worker 里
`fetch('/api/runs/{runId}/snapshots/{stepId}')` 并 `JSON.parse`，主线程完全不接触
大数组。

如果父组件已经有数据（例如复用了别处的请求），可以传 `snapshot`：

```tsx
<WorkflowViewer3D snapshot={snapshotDoc} ... />
```

注意两点：

- `snapshot` **必须是引用稳定的对象**（`useMemo` / state），否则每次父组件渲染都会重新解析；
- 传入的对象会被结构化克隆进 worker（一次内存拷贝）。数据量大时优先用默认的 URL 模式。

### 3. 切换 step

只改 `stepId`（或 `snapshot`）即可。查看器会：

- 命中 `runId:stepId` 的 LRU 缓存（8 条）→ 直接上传几何，约 5–10 ms；
- 未命中 → worker 解析（20 万三角形约 100 ms），期间显示“正在解析快照…”浮层；
- **保留相机**：切换步骤不重新取景；如果新几何明显超出（或远小于）当前取景框，会自动
  适配一次，也可以随时点“适配视图”手动恢复；render window 不会重建；
- 空闲时（`requestIdleCallback`，无则 `setTimeout`）预取前后相邻步骤。

### 4. 2D / 3D 分支

`dimension` 决定用哪个渲染器；如果快照里的几何与它不符（例如 3D 视图拿到没有三角形的
快照），不会硬渲染，而是走 `onStepUnavailable`：

- `3`：vtk.js（动态 `import()`，不进首屏 bundle）。三角网格 + 材料色 + 线框/表面 +
  正交/透视 + 沿 X/Y/Z 的裁剪面 + 朝向坐标轴。切换等轴/XY/XZ/YZ、开关截面都会按
  **当前可见几何**（隐藏材料与裁剪面之后）重新取景，状态栏给出 X/Y/Z 范围与实时比例尺。
- `2`：`Viewer2D` 用 canvas 画 `points` + `lines`，按 `cellData.materialIds` 着色，
  带坐标轴、刻度和比例尺；滚轮缩放、拖动平移、双击复位。**不会**用 3D 引擎假装 2D。

2D 快照的坐标约定与后端一致：横轴 `axisMap.x`，纵轴 `axisMap.z`（ViennaPS 2D 用 x/z，
z 是高度）。

### 5. 错误处理

| 情况 | 界面 | 回调 |
|---|---|---|
| 加载/解析中 | “正在解析快照…”浮层 | — |
| HTTP 404（该步骤没有快照） | “该步骤没有可显示的结构” + 重试 | `onStepUnavailable(reason)` |
| 维度不匹配（3D 视图拿到没有三角形的快照） | 同上 | `onStepUnavailable(reason)` |
| 其它错误（网络/JSON/几何非法） | 错误文案 + 重试 | — |
| vtk.js 加载失败（无 WebGL2 等） | “三维引擎不可用”浮层 | — |

`onStepUnavailable` 适合父组件用来提示“该步骤只能用 2D 查看”或自动切到另一个视图。
重试按钮会重新发起同一次加载。

### 6. 统计与命令式句柄

```ts
onStats?: (stats: {
  triangles: number; points: number; fps: number; parseMs: number;
  stepId?: string; simplified?: boolean; sourceTriangles?: number;
}) => void;

onSceneReady?: (handle: {
  /** 按当前可见几何重新取景（保持视角方向）。 */
  resetView(): void;
  setStandardView(view: 'iso' | 'xy' | 'xz' | 'yz'): void;
  rotate(azimuthDeg: number, elevationDeg: number): void;
  renderNow(): void;
  readPixels(): { width: number; height: number; pixels: Uint8Array } | null;
  getFps(): number;
  getTriangleCount(): number;
  getPointCount(): number;
}) => void;
```

`onStats` 在每次几何上传后触发一次；状态栏打开（默认开）时每 500 ms 再触发一次并带上
实时 FPS。`onSceneReady` 供工具栏或 benchmark 使用（`readPixels` 用于像素断言）。

### 7. 测量标记

`marker` 是可选的三维测量线（红色实线），`axis` 表示**被固定的坐标轴**：

| 配方 | 传参 | 画法 |
|---|---|---|
| `etch_depth`（X 处深度） | `{ axis: 'x', value: x }` | 沿 Z 的竖线，x = value，y 取几何中心 |
| `cd_at_height`（高度处 CD） | `{ axis: 'z', value: height }` | 沿 X 的横线，z = value，y 取几何中心 |

不传就不画。测量线沿**视线方向**前移到大盒之外，因此在正交投影下屏幕位置不变、又不会被
实体挡住；状态栏同时显示 `测量位置：X = … µm`（或高度）。

## 性能设计

1. **一次 draw call**：整个结构只有一个 `vtkActor` / `vtkMapper` / `vtkPolyData`，
   材料用标量 + 查找表着色。切换材料可见性只重建索引缓冲，不新建 actor。
2. **索引化顶点缓冲**：vtk.js 一旦发现 *cell* 标量就会把网格摊平成每三角形 3 个顶点
   （`CellArrayBufferObject.canUseIndexedVBO`）。这里在 worker 里按材料拆分顶点（只复制
   材料边界上的顶点），改用 **point** 标量，20 万三角形从 60 万顶点降到约 12 万，且
   每个三角形的三个标量相同，材料边界依然锐利。
3. **解析在 worker**：`fetch` + `JSON.parse` + 扁平化 + 简化 + 拆分顶点全部在
   `snapshotParse.worker.ts`，结果用 transferable 回传。主线程只做 GPU 上传。
4. **顶点聚类简化**：超过 50 万三角形时在 worker 内做 O(n) 网格 snap（保留 materialId），
   目标 ≤ 30 万；分辨率用 `sqrt(target)` 起步 + 最多 5 次二分，尽量保住细节。
5. **LRU(8) + 空闲预取**：相邻步骤在 `requestIdleCallback` 里预取，切换时命中缓存
   ≈ 7 ms。
6. **rAF 合帧**：所有状态变化（可见性、裁剪、显示模式）只标记 dirty，由
   `requestAnimationFrame` 合并成一次渲染；React state 变化不会重建场景。
7. **相机保持**：场景只创建一次，切步骤只换几何；相机、投影、裁剪面状态都保留。

## 已知限制

- 无头 Chromium 只有 SwiftShader 软件光栅化，20 万三角形在 1254×594 下约 148 ms/帧；
  同机同分辨率的裸 WebGL2 基线是 50 ms/帧，见 `docs/viewer3d-benchmark.md`。真实 GPU
  （RTX 3060 Laptop，Windows Edge）上同一场景是 **16.67 ms/帧（60 FPS）**。
- 面板会读取 `WEBGL_debug_renderer_info`：软件后端（SwiftShader / llvmpipe / Microsoft
  Basic Render）显示可关闭的“软件渲染”提示，但不禁用 3D；`?debug=1` 或勾选“性能”才显示
  完整 renderer 字符串。创建 render window 时显式要求 `powerPreference: 'high-performance'`。
- 背面剔除没有默认打开：开放曲面从背面看会消失，正确性优先。需要时可作为性能开关。
- 2D 只画原生界面线（快照里没有填充多边形），不做体素化。
- 截面控件是**裁剪面**（真实裁剪几何），不是等值面切片；没有截面数据时不会显示假控件。
