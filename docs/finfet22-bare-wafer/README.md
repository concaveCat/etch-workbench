# 22 nm FinFET · 裸片起步 geometry-only demo

**状态：用户已验收（2026-10-31）。** 这是从平坦 Si wafer 起步的 22 nm FinFET
参考 demo，UI integration 不在本目录范围内。

GDS 只作为掩膜版图（Fin 1 / Gate 2 / CB 4 / M0 5），仿真起点是一条
0.5 × 0.5 µm 的平 wafer。

## 标识

| 项 | 值 |
|---|---|
| 项目 ID | `98236be027dd3d2c81c36e9b` |
| 原始 run | `8b13d0e6e3808c567adf8fd5` |
| Replay run | `a0227cc1de1410bb7c21af3a` |
| 结果 | 23/23 步 succeeded；replay succeeded |
| 耗时 | 82 s（CPU） |
| 最终网格 | 349,680 点 / 705,952 三角形 |
| Replay 一致性 | 最终 surface mesh 逐点哈希 `f8622cd7…d6a60a` 完全相同 |
| 版本 tag | `archive/finfet22-bare-wafer-20261031` |

## 起点与尺寸

- 起点：平坦 Si wafer，顶面 z = 0.035 µm，衬底到 z = -0.045 µm。
- 域：0.5 × 0.5 µm，`gridDelta = 0.005`，reflective。
- 7 条 fin：pitch 60 nm、宽 10 nm、STI 以上 35 nm。
- 5 条 gate：pitch 90 nm、长 28 nm、平坦化后高 80 nm。
- 28 个 CB（4 排 × 7 fin），4 条 M0 bar（长 400 nm、宽 24 nm）。

## 流程

| # | 步骤 | 模型 | 类别 |
|---|---|---|---|
| 1 | Pad oxide 30 nm | isotropic deposit | native |
| 2 | Fin 硬掩膜图形 | gdsBoolean difference/outside | geometry-only |
| 3 | STI 沟槽 Si 刻蚀 60 nm | directional | native |
| 4 | STI 填充 300 nm | isotropic deposit | native |
| 5 | STI CMP | planarize cutoff | geometry-only |
| 6 | STI recess 65 nm | isotropic selective | native |
| 7 | Gate oxide 2 nm | isotropic deposit | native |
| 8 | Dummy PolySi 120 nm | isotropic deposit | native |
| 9 | Dummy gate CMP | planarize | geometry-only |
| 10 | Gate 图形 | gdsBoolean difference/outside | geometry-only |
| 11 | Spacer SiN 10 nm | isotropic deposit | native |
| 12 | Spacer 刻蚀 | directional | native |
| 13 | S/D SiGe 选择性外延 15 nm | specialized selectiveEpitaxy | native |
| 14 | ILD SiO2 150 nm | isotropic deposit | native |
| 15 | ILD CMP | planarize cutoff 0.115 | geometry-only |
| 16 | CB 开孔 | gdsBoolean difference/inside | geometry-only |
| 17 | Dummy PolySi 去除 | removeMaterial | geometry-only |
| 18 | IL SiO2 1 nm | isotropic deposit | native |
| 19 | HfO2 2 nm | isotropic deposit | native |
| 20 | TiN 3 nm | isotropic deposit | native |
| 21 | W gate fill 70 nm | isotropic deposit | native |
| 22 | W CMP | planarize cutoff 0.115 | geometry-only |
| 23 | M0 | gdsBoolean union/inside | geometry-only |

14 个 native 工艺步 + 8 个 geometry-only 步 + 1 个材料移除。

## 关键修复

上一版（`dede0529…`）gate 用 `intersection + inside` 加 `0..0.2` 掩膜带，
顶层 level set 的负区一直延伸到衬底，每根 gate 变成从底到顶的 PolySi 柱，
截面是“底座 + 柱 + 横条”。

这一版改为 **`difference` + `retainedRegion=outside`**：只去掉 gate 条纹外的
poly，条纹内保留贴着 fin/STI 起伏的共形膜，fin 穿进 gate。

另外：网格 0.01 → 0.005；fin reveal 4 nm → 35 nm；所有 CMP 改成明确高度切断；
gate 先 dummy CMP 再图形化。

## 截图

- `shots/13-gate-iso-no-oxide.png` — **5 gate × 7 fin 等轴视图（最关键）**
- `shots/01-fin-reveal.png` — 7 条 fin 露出 35 nm
- `shots/12-gate-iso-all.png` — 带 STI 的 gate 等轴
- `shots/16-final-iso-si-sige-w.png` — 最终只留 Si/SiGe/W
- `shots/10-final-xy.png` — 最终顶视
- `shots/04-sd-epi.png`、`06-cb-open.png`、`08-final-gate.png` — 中间截面

## UI（WorkflowApp 01–06）

这条流程已经作为 `finfet22-bare-wafer` starter 接入 01–06 WorkflowApp，并用真实
workflow API 跑通：

| 项 | 值 |
|---|---|
| 项目 | `proj_05ed1c6b482d49399fe1347e` |
| UI 触发的 run | `5f47973510146db794138a05` |
| 步数 | 23/23 succeeded |
| 最终快照 | `bw_m0_pattern`，349,680 点 / 705,952 三角形 |
| 报告 | `report_30f3246347e00432` |
| 验收脚本 | `frontend/tests/workflow-finfet22-acceptance.mjs` |

UI 侧为这条流程做的能力对齐：

- 01 接受后端 0.1 µm 的三维域下限（demo 是 0.5 µm），不再强制 1 µm；
- 02 保留 directional `rateSets`，不再注入标量占位；geometry-only 步骤不再带 `duration`；
- 02 增加 `gdsBoolean.gdsMask`、`planarize`、`removeMaterial`、`specialized.nativeParameters`、directional `rateSets` 的可编辑字段；
- GDS starter 通过真实 Clip/Layout 绑定解析；
- 默认一个 Segment 跑完整 23 步，不再在倒数第二步强制拆段。

截图：`ui/01-project.png`、`ui/02-flow-23-steps.png`、`ui/03-gdsboolean-editor.png`、
`ui/04-run-final.png`、`ui/05-report.png`。

## 已知限制

1. **本 demo 不含 implant/anneal/analysis。** 0.005 µm 网格 + 0.5 µm 域的
   field cell set 在原生 cell-set 路径上 SIGSEGV（`returnCode -11`），所以没有
   把 field 步放进这条流程。原生 field 证据在旧的 0.01 µm run
   `dede05298196d93701e14e58` / replay `4a51322f5e0f517618b92d99`。
2. **S/D SiGe 偏小**（top 0.004 µm、area 0.0098 µm²），没有形成明显的 raised S/D；
   spacer 对 fin 顶面水平 SiN 的清除不彻底。
3. **CB 开孔**后 ILD 面积变化很小，接触孔深度/形状还需要单独确认。
4. GDS boolean 是几何图形转移，不是光刻/选择性刻蚀；planarize 是高度切断，
   不是真正 CMP。
5. STI 填充是 level-set 共形沉积，没有 gap-fill/void 模型。
6. Gate-last 的 HfO2/TiN 是在 CB 开孔之后沉积的，接触孔侧壁也会有一层 liner。
7. Fin 顶是圆/尖头（各向同性 STI recess 造成）；10 nm fin 在 5 nm 网格上只有 2 格。
8. 0.0025 µm 的高分辨率尝试跑完 22/23 步后在 600 s run 上限处超时，**不是**
   验收版本；验收版本是上面的 0.005 µm run。
9. 02 还没有 implant/anneal/analysis 的专用编辑器（本 demo 不使用这三类步骤）；
   这批能力已在后端，但 UI 表单留到下一轮。

## 复现

```bash
cd docs/finfet22-bare-wafer
python3 make_gds_05.py      # 0.5 um 掩膜 GDS
python3 make_flow.py        # flow_front / flow_gate / flow_full.json
# 上传 finfet22_05.gds 到 /api/gds-assets，再把 flow_full.json POST 到 /api/runs
```
