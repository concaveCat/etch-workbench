# Agent 约定（ViennaPS Workbench）

本文件适用于在本仓库内工作的自动化 agent。目标：多个 agent 并行时既不覆盖彼此
的改动，也不抢占同一份运行时资源。

## 1. 改动前先看状态

```bash
git status --short
git log --oneline -3
```

- 工作区干净、改动很小（单文件、文档、只读排查）：可以直接在主工作区进行。
- 主工作区有未提交改动，或任务涉及多个文件、需要启动服务：另开 worktree。
- 拿不准时按“开 worktree”处理。

## 2. 开 worktree

```bash
git worktree add -b task/<短名> /root/wb-<短名> main
cd /root/wb-<短名>
```

worktree 只包含 git 跟踪的源码与文档。下面四项不在 git 里，必须软链到主工作区，否则
`scripts/start.sh` 会因缺少前端产物而拒绝启动：

```bash
ln -s /root/viennaps-workbench-cpu-gpu/.venv .venv
ln -s /root/viennaps-workbench-cpu-gpu/frontend/node_modules frontend/node_modules
ln -s /root/viennaps-workbench-cpu-gpu/frontend/dist frontend/dist
ln -s /root/viennaps-workbench-cpu-gpu/runtime-patched runtime-patched
```

`docs/*-evidence/` 同样不在 git 中。依赖这些证据的验收脚本在 worktree 里会缺文件，
需要时从主工作区只读引用，不要复制后修改。

## 3. 运行时隔离

- 每个 worktree 固定一个端口，从 `8801` 起顺序分配，用 `WORKBENCH_PORT` 指定。
- 数据目录保持 worktree 自己的 `var/`，不要指向主工作区。
- 在主工作区跑测试同样要隔离数据目录（见 §4），因为正式服务占着 `var/workbench.lock`。
- 本机只有一张 GPU：同一时刻只允许一个 agent 跑 GPU 仿真，CPU 长任务也尽量串行。
- 启动前确认主工作区没有正在执行的任务。

## 4. 改完要做什么

```bash
# 1) 测试：必须用仓库内的独立数据目录，否则会和正在运行的服务抢 var/workbench.lock
VIENNAPS_WORKBENCH_DATA_DIR=$PWD/var/test-$(date +%s) WORKBENCH_PORT=8767 \
  .venv/bin/python -m unittest discover -s tests -t . -v

# 2) 前端类型检查（改了前端时）
cd frontend && npm run typecheck

# 3) 提交前
git diff --check
```

跑测试的三条硬约束（都是实际踩过的）：

| 约束 | 原因 |
|---|---|
| 数据目录必须**在仓库内** | `scripts/bounded_child.py` 要求 `--record` / `--log-path` 位于工作台目录内；放 `/tmp` 会直接失败并报 “cwd、record 和 log 必须位于正式工作台目录内” |
| **不要**让数据目录指向主工作区 `var/` | 正式服务（`127.0.0.1:8789`）持有 `var/workbench.lock`，测试 `lifespan` 里的 `flock` 会立刻抛 `another ViennaPS Workbench server already holds the data-directory lock`（约 4 ms 就失败） |
| `WORKBENCH_PORT` 不要用 `8789` / `8769` | 分别是当前正式服务与旧正式服务的端口 |

不要用“先停掉正式服务再跑测试”来绕过第二条：那会打断正在看结果的人，
而且停服务后 60 s 内端口仍处于 TIME_WAIT（见 `/root/.pi/agent/AGENTS.md`）。

历史证据缺失（不是代码回归）：`docs/stage9-evidence/gpu-coverage-audit/.../cpu-nan-debug/worker.guard.json`
没有被归档。`tests/test_stage9.py` 现在由独立用例
`test_historical_cpu_nan_failure_guard_is_archived` 检查：文件存在则断言，缺失则 skip。
不要伪造该证据；重跑 profile-recheck 实验并把真实 guard 归档后可恢复严格断言。

当前基线的预期结果是 **108 项：103 通过 / 0 失败 / 5 跳过**。

此前记录的 3 条红灯已处理：

| 用例 | 处理 |
|---|---|
| `test_project_api_preview_and_bounded_archive` | 断言改为符合“通用 JSON 无固定上限”的 422，并单独用 mock 验证 GDS 64 MiB 的 413 |
| `test_custom_registry_materials_replay_and_native_output_roundtrip` | 修复延迟快照物化丢失 Domain 元数据的问题；参考 writer 改为 compact JSON |
| `test_patched_gpu_multiparticle_multi_ion_support_is_evidence_backed` | 重新生成 eight-ion CPU/GPU 证据；历史 `cpu-nan-debug/worker.guard.json` 未归档，单独用例在缺失时 skip |

跑完若出现新的红，才是真正的回归。

- 提交信息说明“为什么”，中英文均可，与改动文件的文档语言一致。
- 非平凡改动走分支 + PR，不要直接推 `main`。
- 推送前确认 `git status` 里没有误加的运行时文件。

## 5. 不要做的事

- 不要提交 `var/`、`.venv/`、`frontend/node_modules/`、`frontend/dist/`、
  `runtime-patched/`。
- 不要修改或删除 `docs/*-evidence/` 下的证据文件：它们不在 git 中，删除后无法恢复。
- 不要提交 token、密钥、内网地址或主机名；这是一个公开仓库。
- 不要对 `main` 执行 `git push --force`，不要删除其他 agent 的 worktree。

## 6. 收尾

```bash
rm -rf var/test-*                      # 清掉 §4 产生的测试数据目录
git worktree remove /root/wb-<短名>
```

任务放弃或合并后都要清理，避免 `git worktree list` 堆积。
