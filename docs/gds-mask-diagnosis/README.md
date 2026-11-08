# GDS 掩膜破坏多材料结构：原生对照定位

后续实现请使用 [EXECUTOR_TASK.md](EXECUTOR_TASK.md)。其中的固定用户域构造已由
`run_canonical_checks.py` 完成 17/17 个物理与边界检查，结果在
`canonical-acceptance/summary.json`。下文的 matched-grid 实验仍作为根因证据，
不作为产品修复算法；当前指定算法见 `canonical_regrid_probe.py`。

环境：原装 Python 包 ViennaPS 4.6.2、ViennaLS 5.8.5，CPU，单线程。
`native_repro.py` 不导入任何 Workbench 模块。所有测试使用同一原生工艺：
Si 基底顶面 z=0，SiO₂ 顶面 z=5 µm，Mask 高度 5–5.3 µm；
网格间距 0.1 µm；IsotropicProcess 速率 -0.2 µm/s，Mask 速率为零，时间 0.01 s。

## 结论

当前材料丢失的主因是 Workbench 构造了不满足 ViennaPS 嵌套约束的多材料 Domain。
另有一个 Workbench 网格兼容性错误；不兼容网格送入原生 Boolean UNION 时，
原生库会 SIGSEGV。这表现出原生输入校验不足，但不构成合法 GDS 输入无法刻蚀的证据。

### 1. 顶层只包含掩膜，没有包含下层结构

`worker/simulate.py:978` 把独立掩膜体通过
`insertNextLevelSetAsMaterial(extruded, material, False)` 追加到顶层。
False 关闭了默认的下层 UNION，但 extruded 本身没有预先包含下层。

ViennaPS 的材料表示要求：

```
LS0 = Si
LS1 = Si ∪ SiO₂
LS2 = Si ∪ SiO₂ ∪ Mask
```

当前实际的 LS2 只有 Mask。初始表面输出仍可逐层画出三个材料，不能据此证明
材料拓扑正确。ViennaLS Advect 在更新后执行 adjustLowerLayers，将每个下层与
顶层 INTERSECT。于是原本位于掩膜下方的整个 Si/SiO₂ 被裁掉。
这与刻蚀时间、GDS 多边形是否分离无关。

精确版本源码依据：

- [ViennaPS 4.6.2 Domain 约束及插入时 UNION](https://github.com/ViennaTools/ViennaPS/blob/v4.6.2/include/viennaps/psDomain.hpp#L155)
- [ViennaLS 5.8.5 Advect 顶层必须包含下层](https://github.com/ViennaTools/ViennaLS/blob/v5.8.5/include/viennals/lsAdvect.hpp#L41)
- [ViennaLS 5.8.5 adjustLowerLayers 的 INTERSECT](https://github.com/ViennaTools/ViennaLS/blob/v5.8.5/include/viennals/lsAdvect.hpp#L738)

### 2. 掩膜与薄膜的计算网格范围不同

Workbench 的薄膜网格 XY 范围为 [-2.5, 2.5] × [-2.5, 2.5] µm。
原生 GDSGeometry 读回的平面范围为：

```
x: [-8.4700000286, 16.75]
y: [-2.9100000858, 5.6799998283]
```

GDS 筛选器把未选中图形保留在 support layer 上用于原生读入的范围计算，因此
这里包含整个库的辅助范围。Extrude 继承这个网格。Workbench 只沿用了相同的
gridDelta，没有把掩膜转到薄膜的计算网格。

直接把 False 改为 True 会在插入时 UNION 这两个不同网格，复现 SIGSEGV。
无 GDS 的额外对照中，把完全相同的方块建在 6×6 网格再插入 5×5 网格，也在
插入时 SIGSEGV，证实触发因素不依赖 GDS 解析。

## 原生实验矩阵

完整数值保存在 `matrix.json`；每个模式有单独的 `.log`。
“保留”依据是刻蚀后 Si/SiO₂ 表面三角形及其 level set 仍存在，不代表所有工艺
模型或长期刻蚀的精度都已验证。

| 模式 | 网格 | 包裹下层 | 原生结果 |
|---|---|---|---|
| box-unwrapped | 相同 | 关闭 | Si 和 SiO₂ 的 level set 点数均从非零变为 0，只剩 Mask |
| box-wrapped | 相同 | 开启 | Si 和 SiO₂ 各 5,000 个三角形均保留 |
| gds-unwrapped | 不同 | 关闭 | 精确重现 Workbench：Si/SiO₂ 消失，剩 68 点、128 三角形 Mask |
| gds-wrapped | 不同 | 开启 | 插入掩膜时 SIGSEGV，returnCode=-11 |
| gds-matched-wrapped | 相同 | 开启 | 同一 GDS 刻蚀完成，Si 和 SiO₂ 各 44,022 个三角形均保留 |
| box-mismatched-wrapped | 不同 | 开启 | 同一物理方块，只改变网格范围，插入时 SIGSEGV |

`gds-matched-wrapped` 为了单独证明正确的原生构造可行，把两个下层平面建立在
掩膜已有网格上。这个对照扩大了计算域，不能原样作为产品修复。
产品应以用户指定的仿真网格为准，将掩膜转换到该网格，再恢复累积包裹关系。

## 重现命令

在临时工作区根目录执行（每个模式使用独立进程）：

```bash
./.venv/bin/python docs/gds-mask-diagnosis/native_repro.py \
  box-unwrapped \
  var-preview-8792/runs/22b5ffe1169ae6d2799cb31a/gds-mask-selected.gds \
  /tmp/box-unwrapped.json

./.venv/bin/python docs/gds-mask-diagnosis/native_repro.py \
  gds-matched-wrapped \
  var-preview-8792/runs/22b5ffe1169ae6d2799cb31a/gds-mask-selected.gds \
  /tmp/gds-matched-wrapped.json
```

测试使用的 GDS 是 Workbench 实际送入 GDSReader 的同一筛选副本，结构为
sky130_fd_sc_hd__buf_4，原 layer 68/datatype 16（筛选后 datatype 已归零）。
脚本直接使用 GDSReader、GDSGeometry、Extrude、Domain 与 IsotropicProcess。

本轮仅新增诊断文件，没有修改或部署应用代码。
