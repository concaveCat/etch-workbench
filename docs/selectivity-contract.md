# 第3轮：材料相关速率，冻结合同

用户已验收材料/多层结构，授权继续。Luna负责实现，主代理负责debug/独立对照。只做第3轮，不新增DirectionalProcess等模型。

源码 /tmp/viennaps-selectivity-build；交付 /root/viennaps-workbench-selectivity；候选端口8769。原服务8768保留。新增格式4支持2D/3D；旧1/2/3解析、哈希、replay与历史保留。原生4.6.2不升级，所有native调用仍走bounded_child。主代理本轮verify.sh退出2，静态通过/沙箱运行态跳过；不动license/网络/hostname/MAC/loopback0。

## 输入

- ProjectV4与V3结构/材料目录/视图相同，schemaVersion=4，steps用StepV4。
- StepV4沿用id/name/enabled/operation/model/rateMagnitude/duration/depositMaterial/stickingProbability/sourceExponent。
- 新 rateMode:'uniform'|'material'，默认uniform。uniform原语义和角色保护不变。
- material仅允许operation=etch，两种现有模型都支持。rateMagnitude表示未列出材料的默认速率（fallback），新增 materialRates:{原生材料名:非负速率0..2}，显式覆盖指定材料。没有列出的材料用fallback，不能悄悄忽略。
- material模式掩膜角色不覆盖显式材料表；掩膜可设非零速率，零表示保护。native model maskMaterials=[]，再应用材料相关速率。uniform仍按角色保护，与V3一致。
- 材料表只能用已核实目录名字；拒绝bool/NaN/Infinity/负值/超限值、未知字段/材料。uniform不能携带materialRates；material需字段materialRates（可空）。deposit不允许material模式。
- 可选selectivityReference:string，只做UI比值参考；不传给native、不入物理哈希，保存在项目。UI显示参考材料速率/本材料速率，分母0时用字符串∞或保护，不把Infinity写入JSON。
- 进入material模式时UI填入已配置结构材料行：普通材料为当前全局速率，mask角色为0；随后用户编辑的表格是唯一真值，不静默归零。可加/删目录材料行。
- mask角色冲突校验应考虑material模式不使用role自动保护。普通模式仍保持旧保护规则。不支持同材料不同层单独设速率，表格作用于该material的全部区域。

## 执行与兼容

- 严禁继续用单独rateMagnitude>0判断新步骤是否有运动或是否需要粒子引擎/seed：fallback=0而材料表非零仍必须执行；仅fallback和全部行均0为no-op。
- 建立共享纯Python助手（不导入native）处理dict/模型Step的has_motion/uses_flux，覆盖validation/hashing/main/supervisor/worker。frontend也采用相同判定。
- 每个native rate按etch操作转换为负号；0保持0。调用后记录actual default rate与按name/ID的实际有符号速率表。
- 用户结果保留原始Domain网格，schema4 snapshot显式dimension；snapshotDimension/动态材料查看器识别4，不能退回默认2D。
- ProjectV4、replay v4、snapshot v4全链路支持；GPU对新格式仍禁用，CPU必须真实通过。seed/threads具体引擎重放不变。
- Hash包含rateMode/所有适用行及fallback；不包含selectivityReference、行显示顺序、界面层名称/ID、相机；dict排序规范化。旧hash绝不变化。
- v1/2/3查看/保存不自动升级；编辑材料速率时升级4。单纯浏览/相机操作不升级旧格式。

## 验收

主代理将用独立native材料map构造调用对照API：IsotropicProcess(materialRates=有符号map,defaultRate=有符号fallback)；SingleParticleProcess(materialRates=...,stickingProbability=...,sourceExponent=...)后显式setDefaultRate(fallback)。不校准实验物理。

必须覆盖2D/3D、两模型、fallback=0且表非零、掩膜非零、表全部0 no-op、保存重放、未知材料/负值拒绝、旧格式兼容和浏览器材料表/派生比值。构造和具体native接口由主代理受控核实。

分工：后端Luna只改backend/worker/共享纯Pythonhelper/API文档；前端Luna只改frontend/；主代理拥有tests、native probe、debug、验收和部署。
