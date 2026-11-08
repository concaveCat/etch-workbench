# 执行任务：修复 GDS 掩膜加入多层结构后丢失下层材料

## 0. 任务边界与完成定义

在 `/tmp/viennaps-workbench-cpu-gpu-gds-interactive` 实现本文件指定的算法，验证后让
现有 `127.0.0.1:8792` 预览的新运行使用修复。保留临时工作区，合并由用户另行指示。
`/root/viennaps-workbench-cpu-gpu` 是原工作区，不在本任务修改范围。

本任务只修复三维 stack 的 gdsMask 构造和对应回归。沿用当前“导入掩膜→多层→刻蚀”
操作，不改前端、schema、工艺参数或 ViennaPS/ViennaLS 版本。
普通 geometry.kind=gds 多材料路径应审查并列出风险位置，但不在此任务套用本三维
掩膜算法；它包括另外的二维语义，需要独立原生证明。

交付必须同时满足：原输入的计算域和网格间距保持不变；受保护区域符合掩膜极性；
短刻蚀保留下层材料；裸露区域退蚀量符合速率×时间；程序无 SIGSEGV。
不能用 succeeded 或三角形数量非零单独判断完成。

执行前读 `/root/AGENTS.md`、`/root/.pi/agent/AGENTS.md`，运行 `/opt/eda-patches/verify.sh`。
退出码 2 表示沙箱跳过运行态，须如实记录。不要修改网络、license 或已部署的 EDA 安装。
当前临时工作区是普通目录副本，没有 `.git`。先把每个待改已有文件复制到本目录下
`backups/<时间戳>/`，不要用 git reset/checkout 代替备份。

## 1. 已决定的根因与修复位置

主要入口为 `worker/simulate.py` 的 `_apply_gds_stack_mask()`。
当前代码把独立的 extruded Mask 用
`domain.insertNextLevelSetAsMaterial(extruded, material, False)` 追加到顶层。
这使顶层只有 Mask，违反顶层应包含全部下层的约束。原生 Advect 更新后与顶层取交集，
Si 和 SiO₂ 因此变空。另一个问题是 GDS 掩膜网格来自库范围，与用户计算域不一致；
直接改 True 会在原生 UNION 中崩溃。

正确的不变量是：

```
LS0 = Si
LS1 = Si ∪ SiO₂
LS2 = Si ∪ SiO₂ ∪ Mask
```

具体根因证据见 `README.md`、`matrix.json`。`False` 本身不是全局禁用参数；
复制已经累积的 level set 等调用仍可合法使用它，禁止全文件替换 False。

## 2. 技术决策已经完成：照此实现

原生证明位于 `canonical_regrid_probe.py`。它不导入 Workbench，安装版本为
ViennaPS 4.6.2 / ViennaLS 5.8.5。`canonical-acceptance/summary.json` 记录 17/17 通过。
只采用该脚本当前的构造顺序，不采用历史 `planar-*`、`volume-*` 或早期 `canonical-*`
日志中的候选算法。

### 2.1 新增 helper，主入口只负责文件、材料和记录

新增 `worker/gds_mask.py`，定义：

```python
def build_aligned_gds_mask(ps, domain, planar_inside, *, base_height, height,
                           retained_region):
    # 返回 (aligned_mask, diagnostics)，不向 domain 插入材料。
    ...
```

helper 不导入 viennaps 顶层包，不做文件读写或材料注册；使用传入的 ps。
`domain` 是已经建立好基底、薄膜的真实仿真域。
`planar_inside` 是原生 GDSGeometry.layerToLevelSet 读出的多边形内部，尚未 INVERT。
诊断用的表面提取必须在 level set 深拷贝上做。

### 2.2 主入口保持原生 GDS 读取

保留现有文件哈希检查、structure/layer/datatype 筛选、单位和 XY 放置逻辑。
不要手写替代 GDS 多边形布尔器，也不要更换 backend/gds.py 的 support-layer 策略。

读取中间几何使用两个 REFLECTIVE_BOUNDARY，与最终计算域的周期边界解耦：

```python
geometry = ps.d2.GDSGeometry(delta)
geometry.setBoundaryConditions([reflective, reflective])
ps.d2.GDSReader(geometry, str(filtered)).apply()
planar_inside = geometry.layerToLevelSet(layer_id, True)
```

此处第二个参数 True 是二维 API 的 blurLayer，不是反选。
删除原入口对 outside 的二维 INVERT，反选由 helper 的有限薄层差集实现。

### 2.3 helper 的唯一指定算法

1. 用原生 Extrude 把 planar_inside 拉伸到 `[base_height, base_height + height]`。
   中间几何边界为 `[reflective, reflective, infinite]`。
2. 必须补足有限厚度的上下封口：
   - 从 extruded 分别深拷贝得到 upper、lower，保证继承同一源网格。
   - 用 `ps.ls.d3.MakeGeometry(..., ps.ls.d3.Plane(...))` 将 upper 重建成 z=top 的下半空间，
     lower 重建成 z=base 的下半空间，法向均为 `[0,0,1]`。
   - `upper RELATIVE_COMPLEMENT lower` 得到有限厚度 source_slab。
   - `extruded INTERSECT source_slab`，保证大面积多边形内部也有完整上下表面。
3. 在 extruded 的深拷贝上用 `ToSurfaceMesh(copy, mesh, 0.01)` 提取三角面。
4. 创建空目标：`aligned = ps.ls.d3.Domain(domain.getGrid())`。
   必须直接复用真实计算域的网格，不能从 GDS bounds 重建、不能扩大用户计算域。
5. 调用 `ps.ls.d3.FromSurfaceMesh(aligned, mesh)`；
   `setRemoveBoundaryTriangles(False)` 后 `apply()`。
   已核对原生实现：False 对周期方向应用周期边界；非周期方向仍丢弃全在域外的面片。
   不自行改变坐标或做 modulo 运算。
6. inside：直接使用 aligned。
   outside：在真实仿真网格上用 `GeometryFactory(domain.getSetup(), name).makeSubstrate()`
   建 top、base 两个下半空间，以 RELATIVE_COMPLEMENT 构造 target_slab，
   再做 `target_slab RELATIVE_COMPLEMENT aligned`。如果 aligned 内部为空，outside
   就是完整 target_slab。这一步全部使用兼容的真实计算网格。
7. 最终返回体为空（getNumberOfPoints()==0）时抛出 RuntimeError：
   `GDS 掩膜在当前计算域内为空，请调整版图位置、保留区域或计算域。`
   空检查必须在反选之后；全域覆盖的 inside 掩膜是有效输入。
8. 返回独立有限掩膜及 diagnostics。最终主入口执行：
   `domain.insertNextLevelSetAsMaterial(aligned_mask, material_handle(...), True)`。

源 slab 的两次 MakeGeometry 和布尔运算，以及目标 slab 的构造，API 调用示例均已在
`canonical_regrid_probe.py` 中实测；执行者不再选择其他重网格方案。

### 2.4 需要保留的行为

- base_height 仍包含原输入 position[2]，单位仍为 µm，height 不变。
- 反射域按用户实际计算网格裁切；周期域按用户实际周期重复。
- 一个图形在计算域外时，不能自动居中、放大计算域或修改 gridDelta。
- 源 GDS 中本来分离的岛仍保持分离，不把它们强行连接。
- `protectsEtch`、材料注册、独立 GDS 掩膜层及顶层图案的现有含义保持不变。
- helper 不调用 Domain.getBoundingBox，不通过副作用检查修改真实仿真域。
- 不新增 UI 气泡、资源提示或新参数。空几何作为运行失败的明确错误返回。

## 3. 修改顺序与每步闸门

### A. 固定环境和基线

记录 Python 包版本与 `/opt/eda-patches/verify.sh` 结果，备份 `worker/simulate.py`。
先运行：

```bash
./.venv/bin/python docs/gds-mask-diagnosis/run_canonical_checks.py
```

必须看到 17 个 PASS，且 `canonical-acceptance/summary.json` 的 passed=cases=17。
该脚本每个案例启动独立受监管进程，45 秒/2 GiB；包含原 0.03 µm 网格。
如果本机基线未通过，保存 guard 和日志，先报告环境/版本差异，不改算法绕过失败。

### B. 实现 helper 和接入入口

只新增 `worker/gds_mask.py` 并修改 `_apply_gds_stack_mask()`。
保留原 transform/readback 字段；新增实际构造诊断：
`gridSource=simulation-domain`、`wrapLowerLevelSet=true`、`alignedMaskPointCount`、
`sourceSurfacePointCount`、`sourceSurfaceTriangleCount`、`retainedRegion`、
`construction=native-extrude-capped-surface-regrid-union-v1`。
`nativeGeometry` 字符串应如实包含 FromSurfaceMesh 和 UNION。
原有 extrudedPointCount 保留为原始拉伸体点数，不偷换成并集后的累计点数。

完成本步后做 Python 语法检查。不要为使源码断言通过而伪造 readback 字段。

### C. 添加调用产品 helper 的物理回归

新增 `tests/test_gds_stack_mask.py` 与所需独立进程 fixture。
测试必须调用产品 helper，不能只再次运行独立原生证明然后宣布产品已修复。
可以复用原生证明的场景和取样方法，检查以下条件：

- 三种材料在短刻蚀后仍有有效表面，下层 level set 点数非零。
- 用户域/边界/gridDelta 前后一致；不以三角形数量完全相等作要求。
- 方块内部保护点 SiO₂ 高度为 5.0，远离掩膜的裸露点为 4.96；
  rate=-0.2、duration=0.2，平坦取样点容差 1e-5 µm。
- outside 极性使上述取样结果交换；环形孔洞的内外关系正确。
- 全域覆盖 inside 有效，完全域外 inside 为空错误；全域覆盖 outside 为空错误。
- 反射跨边界只保留域内部分；周期跨边界两侧保护，完全在域外的周期图形能映回。
- 非整除网格案例 delta=.07、extent=5.07 正常。
- 实际 GDS delta=.1 和 .03、duration=.01 和 .2 均不丢失下层材料。

若比较掩膜边缘/顶面尺寸，容差最多 2*gridDelta；平坦取样点要离轮廓至少两个网格。
实际 buf_4 的小多边形在 .1 网格下不足两个间隔，不用它断言精细轮廓精度。

建议命令：

```bash
./.venv/bin/python -m unittest tests.test_gds_stack_mask -v
./.venv/bin/python -m unittest tests.test_vtk_legacy tests.test_selectivity tests.test_geometry_operations -v
```

所有新物理测试通过后执行相关已有检查。若已有参考构造器与产品使用相同错误套路，
不能仅凭“产品与参考相等”判通过；保留独立的速率/取样/材料不变量检查。
遇到旧测试要求错误几何时，记录差异和依据后再修正对应期望，不能批量刷新快照。

### D. 当前 WebUI 运行路径验收

worker 是每次运行单独启动的脚本，本任务无需修改/重启 backend 或重新构建前端。
通过现有 `8792` API 提交新运行，不修改旧运行文件。

先生成完整请求文件，下面的代码可直接执行：

```bash
./.venv/bin/python - <<'PY'
import copy, json
from pathlib import Path
base = json.loads(Path('var-preview-8792/runs/22b5ffe1169ae6d2799cb31a/replay-project.json').read_text())['normalizedProject']
out = Path('var-ui-verify/gds-mask-fix')
out.mkdir(parents=True, exist_ok=True)
for name, delta in [('mask-01', .1), ('mask-003', .03), ('plane-control', .1)]:
    project = copy.deepcopy(base)
    project['domain']['gridDelta'] = delta
    project['steps'][0]['duration'] = .2
    if name == 'plane-control':
        geometry = project['geometry']
        geometry.pop('gdsMask')
        geometry['layers'] = [row for row in geometry['layers'] if row.get('source') != 'gds-mask']
        geometry['pattern'] = {'kind': 'none'}
    (out / f'{name}.request.json').write_text(json.dumps({'kind': 'simulate', 'project': project}, ensure_ascii=False))
PY
curl --max-time 10 -sS -X POST -H 'Content-Type: application/json' \
  --data-binary @var-ui-verify/gds-mask-fix/mask-01.request.json \
  http://127.0.0.1:8792/api/runs \
  -o var-ui-verify/gds-mask-fix/mask-01.response.json
```

首例完成后再以相同命令依次提交 mask-003、plane-control 文件，分别保存 response。
若 API 返回 409，等待现有运行结束；不取消用户运行。

现有原始 GDS assetId 已在此预览的 storage 中。提交后记录返回的 runId；每秒读取
`var-preview-8792/runs/<runId>/manifest.json`，最多等 120 秒。
检查 run.log、child-record.json、初始和最终快照。实际 final stepId 为
`step_30aeb79d7ef049758fda719c`。

必须满足 returnCode=0、status=succeeded、Si/SiO₂/Mask 都存在，以及裸露平面高度
约 4.96 µm、计算域未改变。旧错误运行最终只有 68 点/128 个 Mask 三角形；
新的结果必须通过材料和物理检查，而不是要求某个指定的点数。
plane-control 就是三维 stack 不带 GDS 掩膜的平面刻蚀对照，同样检查裸露面为 4.96。

### E. 审查与交付

用 rg 搜索其余 `insertNextLevelSetAsMaterial` 调用，记录普通 GDS 多材料路径中的
独立几何直接追加问题；本轮不全局替换，不改二维普通 GDS 构造。
记录 GPU/粒子模型是否实际回归；未运行的模型不得写成已通过。
本次强制验收为当前出现问题的 CPU isotropic 路径，修复不依赖 CUDA 可用性。

最后交付：修改文件清单、备份位置、测试命令和结果、两个修复后 WebUI runId、
一个无掩膜对照 runId、剩余风险。临时工作区保留，原工作区和网络/license 不动。

## 4. 禁止的捷径与失败处理

- 不单独把 False 改 True；不让下层改用 GDS 库网格；不调粗用户网格来掩盖崩溃。
- 不跳过 Process.apply；不删除下层材料；不只绘制初始快照充当刻蚀结果。
- 不使用二维轮廓重建作为当前修复：它会把完全覆盖计算域的图形误判为空。
- 不使用“二维 INVERT → Extrude”作为反选；本任务已经决定用有限三维 slab 差集。
- 不省略源 extruded 与有限 slab 的 INTERSECT：未补封口的候选在全域覆盖案例失败。
- 不升级原生库或改其二进制。若固定 API 不存在或上述强制检查失败，保留代码、
  原始日志和 guard 记录，报告具体失败案例，停止发布并等待审查；不能自行放宽验收。

本任务书仅决定并证明实现路线。生成任务书时，应用 worker/前端尚未按此方案修改。
