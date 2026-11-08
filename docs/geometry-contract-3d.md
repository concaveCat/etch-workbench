# ViennaPS 4.6.2 三维几何合同

本合同来自安装版本的受控实际运行，证据见 `var/evidence/g1/results.json`、`g1-r2-record.json`、`t02-t05.json`、`t11-sections.json`。不是从 4.7 源码推断的能力。

- 原生维度显式为 3，Domain 显式使用 `(gridDelta,xExtent,yExtent,boundaryType)` 四参数构造。非对称 6×4 µm 实例的表面横向包络为 `[-3,3,-2,2]`，物理轴为原生坐标 0/1/2，即 x/y/z。
- x、y 共同设为 reflective 或 periodic；几何生成后的边界读回分别为 `0,0,1` / `2,2,1`，最后一轴无限。z 没有伪造的有限范围。
- 网格间距读回为请求值。非整数网格范围可能由引擎离散化；保存请求范围、实际表面范围及原生活动窄带包络。周期窄带上界可能少一个网格节点，这不等于缺失周期域或互换物理轴。
- 平面高度是 z 高度。无掩膜的三维沟槽沿 y 方向贯穿横向范围，width 是 x 开口宽度，depth 从 z=0 向下测量。
- 圆孔调用原生 MakeHole，显式 `HoleShape.FULL`、Si 与 Mask 材料。radius 是孔半径，深度沿负 z，完整孔覆盖四象限。
- 正的孔/槽锥角使底部收窄：孔在 z<0 的名义半径为 `radius + z*tan(angle)`；槽名义宽度为 `width + 2*z*tan(angle)`。正 maskTaperAngle 使掩膜开口向上扩大。初始几何测试覆盖 ±10°、0° 和两种边界。
- 采用固定离散几何容差 `2*gridDelta`。截面 z=0 附近的界面节点可存在小于一格的偏移（已测孔中心交线约 0.00035 µm）；测量以真实离散表面为准，不强行投影/补造理想曲面。
- Mask/Si/SiO2 的原生 ID 为 0/10/30。getTriangles 的单元顺序与原始 ASCII VTK type=5 面单元顺序、MaterialIds cell array 完全对应。没有将材料数组作为点标量插值，也没有丢掉非三角形再错误索引；如遇非三角形表面本版明确失败。
- 只使用 `addInterfaces=true, sharpCorners=false, minNodeDistanceFactor=0.01`。相邻同材料 level set 在仅用于输出的 Domain 合并内部界面；模拟 Domain 保留全部 level set 及快照局部索引，不推断稳定原生层 ID。
- 原生 ASCII VTK 写出精度有限，已测与规范 JSON 对应顶点误差不超过 5e-6 µm，独立读取容差固定 1e-4 µm。JSON 的 CPU 同线程同种子重放容差为 `max(1e-8,1e-4*gridDelta)`，已测结果为 0。
- 截面为三角形与平面的交线。跳过共面三角形，对重复边按材料身份去重，不填充未知体积，不自动封闭裁剪边界。
