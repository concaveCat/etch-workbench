# API / 文件增量合同 v2

原 `api-contract.md`、`schema.json` 的 v1 二维语义保留。新二维仍创建 v1；三维使用 schemaVersion=2、dimension=3。项目 API 接受两个版本并拒绝未知版本与未知字段。文件保存及导入不批量转换旧文件。

规范 Schema：

- `schema-project-union.json`：项目 v1/v2 联合类型。
- `schema-snapshot-v2.json`：三维面快照。
- `schema-replay-v2.json`：三维重放封套。
- `tests/fixtures/project-3d.json`：可导入网页的两步真实圆孔范例。

三维 domain 为 gridDelta、xExtent、yExtent、boundaryXY（reflective/periodic）。几何支持 plane/trench/hole；孔使用 radius。solver 与步骤字段保留 v1 含义。view3D 保存 projection、camera、displayMode、visibleMaterialIds、opacityByMaterial、clip、section；camera 为 null/省略时首次 fit，其余视图字段有明确默认值。具体字段与范围以 Schema 为准。view3D 不进入任何物理输入哈希。

三维快照显式 dimension=3、topology=triangles，points=N×3，triangles=M×3，cellData.materialIds=M 项；mesh 使用 pointCount、triangleCount、topology、vtk。surfaceBounds 是有限表面包络，domainReadback 明确请求范围、实际活动窄带/表面范围及无限边界标签。不得将 triangles 填进 lines，或把三维计数放进 lineCount。快照还可选带 `materialTops`（各材料上表面的最高/中位/90 分位高度与面积）与 `measuredRemoval`/`measuredAdded`（该步实测去除/增加厚度，按面积加权中位暴露面计算），语义见 `cmp-planarize-contract.md`。

旧快照只有在 schemaVersion=1 时才允许省略 dimension 并按二维读取；浏览器依据选中快照维度选择查看器。跨二维/三维叠加比较禁用。三维比较在同一个 renderer 内使用共同世界坐标、相机及裁剪。

v1 requestedInputHash 的规范化字段及序列化算法不变。v2 使用同一规范 JSON 编码器，但包含真实 dimension=3、x/y 范围、boundaryXY 和三维几何。名称、ID、视图、禁用步骤不进入哈希，步骤顺序和适用数值设置进入。executionHash 包含每步具体引擎、种子、线程和实际包版本。

v2 replay 封套与 normalizedProject 版本必须匹配。每个启用且非零的 singleParticle 步骤必须且只能有一条具体引擎/种子要求；每项按维度、模型、操作、边界核实。v2 可以按步指定不同已验证引擎；v1 保留单一粒子引擎要求。一个运行解析一个实际种子，并分别初始化各粒子步骤，replay 固定该种子。零速率与 isotropic 不要求粒子引擎。版本/线程/种子不符或组合不可用时返回 422，不降级。

运行 API、项目存储、取消、日志、ZIP 的地址及生命周期保持不变。三维能力见 `/api/capabilities` 的 capabilities.threeDimensional.rows；三维资源策略见 resourcePolicy.threeDimensional。auto 为已核实 CPU 优先；全局 gpu 偏好要求每一个有效粒子步骤的组合均已通过。三维 GPU 行通过 `scripts/probe-host-gpu.py` 逐组合受控核实；沙箱 `cuInit` 失败单独记录为环境限制，不能据此屏蔽宿主能力。未通过的行保持关闭；能力文件标明已核实设备和证据。

应用不设固定网格点数、三角形数或单快照 JSON 大小上限；网格间距只要求为正且不超过 0.5 µm。浏览器显示会按视图密度抽样网格辅助线，但返回的模拟网格不会抽样。计算仍受 bounded child 的运行时限、best-effort RSS 监测、日志、单次产物、单活动任务及工作台存储准入保护；RSS 监测不统计 CUDA 显存。
