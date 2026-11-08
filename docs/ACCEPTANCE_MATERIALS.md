# 主代理自动验收：材料与多层结构

状态：自动验收通过，等待用户配置体验验收。终点为计划第1—2轮。

## 实现和职责

- GPT-6 Luna backend 子代理：v3项目与目录API、worker材料解析和多层结构。
- GPT-6 Luna frontend 子代理：动态材料选择、层编辑器、二维/三维动态材料查看。
- 主代理：原生接口核实、代码审阅、修复、独立参考调用、API/浏览器/回归验收及交付。

## 证据

- materials-catalog.json：13项真实注册表名称、ID与颜色。
- materials-evidence/native-probe-results.json：二维/三维层叠与顶层槽口。
- materials-evidence/multiple-mask-probe.json：两模型、两维度的多个真实掩膜材料名单。
- materials-evidence/stack-pattern-probe.json：9组槽/孔和0、正、负锥角原生调用。
- materials-evidence/*-comparison.json：11组独立原生参考，19个快照坐标最大差0，连接和材料数组完全一致。
- materials-evidence/acceptance.log：本轮4个测试方法，包含多组真实API运行、直接参考、保存重放和VTK核对。
- materials-evidence/regression-fixed.log：29个测试方法，28通过，1个已有GPU宿主用例跳过。
- materials-evidence/browser-acceptance.json：6项真实浏览器操作检查通过，无页面JavaScript异常。
- materials-evidence/materials-3d.png：三维材料和层编辑器截图。

验收标准是界面输入与ViennaPS实际调用、结果传回的一致性，不评价实验校准或ViennaPS物理模型本身。

## 主代理修复记录

1. 候选复制规则漏掉依赖包内部dist目录；从原依赖目录完整恢复，构建通过，无新增全局依赖。
2. 本机BooleanOperationType别名不存在；核实使用ps.ls.BooleanOperationEnum。
3. 新格式steps为规范化字典，旧simulate检查用属性访问导致异常；改为按版本兼容访问。
4. 几何检查仍固定Si ID 10；改用选择的真实基底材料ID，PolySi掩膜结构对照通过。
5. 多层厚度、孔/槽范围和高度预算不足；补联合校验，并把默认层厚调整到网格可表达范围。
6. 图层重命名/ID曾进入物理哈希；排除界面标识，保留材料/厚度/顺序/角色。
7. 添加/复制层曾改变已有角色；修复为保留已有材料角色，失效开口通过明确提示关闭。
8. 旧三材料显隐名单隐藏新材料；v3动态目录初始化与显式显隐列表，旧格式兼容。
9. 下拉框的可访问名称混入选项文本；为控件添加明确aria-label，真实浏览器检查通过。
10. 旧回归测试把schema3视为未来格式、把已核实旧GPU当不可用；更新为真正未知schema99和未开放GPU_LINE/受控能力拒绝夹具，保留运行语义。

## 交付边界

新v3原始网格来自simulation Domain，不合并同材料内部界面。旧格式和历史不回写。
新材料/层叠CPU已验证；GPU未作为本轮新组合开放。未增加按材料速率、更多模型或后续阶段功能。

源码持久目录：/root/viennaps-workbench-materials。
候选端口：8768。旧三维目录与服务保留。等待用户验收后才继续下一阶段。
