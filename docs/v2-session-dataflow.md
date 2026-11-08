# v2 Run Session：每步 replay 不变，run 级对象复用

## 目标

在不改变 worker 每步执行边界的前提下，去掉每个 step 重复构建的 run 级对象：

- plugin registry（含 lock 校验）
- engine provider
- native `StateBackend`
- cell-set backend
- `FlowSession` 本身

每步仍然独立执行、独立 snapshot、独立落盘。`<run_dir>/v2/<step_id>/` 下的
`state.json`、`outputs.json`、`plan.json`、`geometry.vpsd`、`fields.npz` 和
`flow.json` 仍是该 step 的 replay artifact。

这不是 warm pool：session 只活在单个 worker run 内；worker 退出后所有 run 级对象
释放。它也不改变 `mutates_input` 的 checkpoint 语义。

## 分层

```text
WorkerV2Session                 # 一个 run 一个
  ├─ ProcessRegistry             # 加载/校验一次
  ├─ EngineProvider              # engine.toml + engines.lock 组合，一次
  │    └─ EngineLease            # handle / state / surface / cell-set
  └─ FlowSession                 # 每步一个 FlowExecutor，复用上面的对象
       └─ StateStore             # 每步一个 <run_dir>/v2/<step_id>
```

`FlowSession.run_step()` 只执行一个 `FlowStep`，不会把整个 run 批处理成一个
`FlowExecutor.run()`。这样：

- worker 仍能在每步之后 `save_snapshot()`；
- GPU context reuse / teardown 仍按原步骤边界决策；
- `ctx.domain` / `WorkerV2State` 仍在每步 commit 后更新；
- per-step replay 文件布局不变。

## 正确性约束

1. **registry 一致性**：dispatch 与 execution 必须使用同一个 registry。worker
   的 `ensure_session()` 返回的 `session.registry` 同时用于 `dispatch_step()` 和
   `run_v2_claimed_step()`；传入不同 registry 是硬错误，不会静默混用。
2. **session 身份**：session key 包含 run dir、ViennaPS 运行时版本、
   engine fingerprint、GPU UUID；任一变化都会重建 session。
3. **每步仍 checkpoint**：`mutates_input` 仍由 `FlowExecutor` 在每步执行前复制一次
   私有 geometry；本次改动只去掉重复的 registry/provider/backend 构建，不移除
   回滚保护。
4. **每步仍落盘**：`FlowSession` 只复用 run 级对象，`FlowExecutor` 仍为每个
   step 写 `state/outputs/plan/geometry`。
5. **失败语义不变**：解析、profile、material、state contract 的失败仍在执行前
   fail closed；不引入 CPU/GPU 静默 fallback。

## 后续可做（不属于本切片）

- 把 `StateStore` 提升到 run 级，同时保留 `v2/<step_id>` 子目录布局；
- 每步 artifact 的二进制/压缩编码；
- 内容寻址的 level-set/field blob 去重；
- `preview` 从 committed state 投影，避免单独的 full-state 副本。
