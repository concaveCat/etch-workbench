# 工程风格与契约

本文件只约束**新增/重构**的模块，不要求回头改写历史文件。改动前先读本文件与
`AGENTS.md`。

## 代码落位

- 新的 step model 放进 `worker/steps/`，实现一个 executor（参考 `context.RunContext`
  与 `geometry.py` / `cellset.py` / `noop.py`）。只有当 executor 确实表达不了时才允许在
  `execute()` 里加分支。
- 新的 API 资源放进 `backend/api/`（路由）或 `backend/packages.py`（可复用流程/包）。
  新逻辑一律不要写进 `backend/main.py`：它是组装根，只保留已有的历史路由与再导出。
- 共享常量只有一份。GPU 多粒子/多离子策略字符串以 `backend/validation.py` 为准，
  worker 侧通过 import 引用，不再本地复制。
- 新的前端状态放进 `frontend/src/features/` 或 `frontend/src/hooks/`，不要继续
  扩大 `App.tsx`。

## 契约不可变

任何重构（拆分模块、移动函数、改 import）都**不得**改变：

- snapshot JSON 的字段、值与序列化顺序；
- 事件（event）的顺序与内容；
- native ViennaPS 调用顺序与参数；
- 任何已有哈希、指纹或 capability 判定结果。

需要改变以上任一项时，视为行为变更，单独提交并附带证据，不要混在 refactor 里。

## 提交前自检

```bash
bash scripts/check-style.sh          # compileall + 新模块 ruff + 前端 typecheck + diff --check
                                    # 缺 ruff 会直接失败：.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest tests.test_contract_parity tests.test_native_multiion_policy -v
```

完整 Python 套件需要独立数据目录和 `docs/*-evidence/`、`.venv`、`frontend/dist`、
`runtime-patched` 的软链（见 `AGENTS.md`），不要在正式服务的 `var/` 上直接跑。
