# 材料与多层结构：本轮冻结合同

本轮仅完成第 1—2 轮，完成后等待用户验收。不要新增按材料速率表、定向刻蚀、平坦化或其他模型。

开发根目录 /tmp/viennaps-materials-build；交付持久目录 /root/viennaps-workbench-materials；候选端口 8768。原三维应用只读，不操作其服务/数据。已有依赖已复制，不升级原生包。所有 ViennaPS 导入/运行通过 scripts/bounded_child.py；验收是 API 参数/输出保真，不校准实验物理。

## 固定输入

- 旧 ProjectV1(schema1,2D)、ProjectV2(schema2,3D) 解析与哈希保持兼容。
- 新 ProjectV3：schemaVersion=3，dimension=2|3，name/units/domain/solver 沿用旧字段；2D 用 view，3D 用 view3D。严禁同材料 ID 的掩膜角色与其他角色冲突被静默处理。
- geometry 为 plane/trench/hole/stack。前三者沿用旧参数，substrateMaterial 为真实材料名（如 Si3N4），trench/hole 增加 maskMaterial，默认 Mask；maskHeight=0 不启用保护。hole 仍只支持3D。
- stack geometry 精确字段：kind='stack', substrateMaterial: string, baseHeight: number(默认0), layers: [{id:string,name:string,material:string,thickness:number,role:'film'|'mask'}], pattern: {kind:'none'} | {kind:'trench',width:number,taperAngle:number} | {kind:'hole',radius:number,taperAngle:number}。层自底向上。最多16层。仅最顶层可开口，开口要求顶层 role='mask'；2D 禁止 hole。无层时只允许无图案。
- stack 基底到 baseHeight 为半无限体；各层厚度累计；图案只切顶层掩膜，不预先切穿下面薄膜。native 构造方式和锥角由子进程核实。
- StepV3 仍只有 etch/deposit、isotropic/singleParticle；depositMaterial 改为材料名，刻蚀不带此字段，其余沿用。没有本轮新增材料速率控制。
- 掩膜 role 与物理材料身份分开：保护由真实材料名单传给 native 模型。材料不同可以同角色；同材料兼有掩膜和非掩膜会产生原生 material ID 歧义，提交时明确拒绝。对被保护材料的非零沉积同样明确拒绝。界面说明这些是当前简化模型的限制。
- 所有新材料名字通过本机核实材料目录解析成真实 native handle，不接受任意 ID。目录优先支持 Mask,Si,SiO2,Si3N4,SiN,PolySi,SiGe,Al2O3,HfO2,TiN,W,Cu,PMMA 中实际存在项。最低必需 Si/SiO2/Si3N4/PolySi/TiN/W。不注册假化学参数、不新增用户自定义材料。
- GET /api/materials 返回 {schemaVersion:1,engineVersion:'4.6.2',materials:[{name:string,id:number,color:string}]}；供前端共享。目录持久于 docs/materials-catalog.json，原生句柄在 worker 解析。
- 选择材料不自动改变通用模型的速率/黏附概率。用户可看到模型当前仍采用全局设置。

## 固定输出与界面

- 新快照 schemaVersion=3，dimension 显式2或3，拓扑按维度 lines/triangles，材料名字/颜色从真实目录来，不允许三材料白名单。ID、快照局部 LS 索引、编辑层 ID 分开。
- v3 保存原始 simulation domain 的 getSurfaceMesh 输出及原始 VTK，不调用 material_surface_domain 合并/重建。sameMaterialInternalInterfacesHidden=false，查看器如需过滤必须明确标为显示过滤，不能改变原始导出。旧版本处理保持不变。
- 重复同材料层在编辑列表独立存在，原生输出是否保留内部界面如实显示，不捏造稳定原生层ID。
- 初始预览显示请求层次/厚度与 native material map；用原生输出作双向验收，不能凭颜色或自填JSON判定成功。
- 旧v1/v2 replay 保持；v3 replay可保存/导入、固定真实引擎和种子，新结构参与哈希；view/名称不入物理哈希。
- 原生CPU全流程必须通过，新材料/stack GPU 未重新核实前禁用或拒绝，不继承旧 GPU 数据放行。
- 前端材料目录下拉覆盖基底、掩膜、沉积；stack 面板含层增删复制上下移动、材料、厚度、角色、顶层图案；2D/3D均可预览、保存/重开、导出重放。
- 保留现有查看器/切片/比较；从快照元数据生成材料颜色图例与显隐项，去除固定[0,10,30]限制。

开发分工：后端子代理只改 backend/、worker/、材料目录与API文档，不改 frontend/、tests/。前端子代理只改 frontend/。主代理负责核实native能力、独立直接调用对照、测试、debug、交付运行与用户验收说明。
