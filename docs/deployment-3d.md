# 本地部署 / 安全数据复制 / 回滚

开发基线 `/root/viennaps-workbench` 保留，候选 `/root/viennaps-workbench-3d` 使用独立 `.venv`、前端 node_modules、var、锁和 8767。最早 G1 证据中记录的 cwd 位于临时方案子目录；随后已迁入指定候选目录，历史证据不篡改。该临时源码快照不是运行入口。

所有命令应在能监听 localhost 的宿主终端执行。执行沙箱的 EPERM 不能用于宣称服务或浏览器已验收。

1. `./scripts/build-frontend.sh`：从锁文件离线重建。
2. `./scripts/verify-browser-host.sh`：只启动候选自己的服务到 8767，真实浏览器执行创建、预览、多步、材料、相机、剖切、比较、PNG、保存、重开、重放、ZIP、旧二维历史和快照切换内存检查，写入 `var/evidence/browser-3d.json`。
3. 读取结果。只有 status=passed 且后端必需测试通过，才可执行 `./.venv/bin/python scripts/promote.py`。
4. promote 检查两套服务均无活动计算，分别调用各自身份校验的停止脚本；旧目录原样保留。复制仅限旧 var/projects 和 var/runs，排除服务锁、server.json、PID、就绪文件；历史子进程记录仅保留为证据。
5. 每一文件均核对 SHA-256；同 ID 同内容可保留，异内容碰撞先失败，绝不盲覆盖。源目录复制前后校验一致；禁止两服务写相同数据目录。
6. 新候选在 8766 启动，原服务监听已由其自身脚本释放；端口若被未知服务占用则失败并恢复原服务，不杀未知进程。结果写 `var/evidence/migration-copy.json` 和 `deployment-3d.json`。

新版日常启动默认使用 `var/listen-port.json`（切换时保存的端口），没有此配置时用 8767。WORKBENCH_PORT 可显式覆盖。健康、Host 和 Origin 使用同一实际端口。

回滚：`./.venv/bin/python scripts/promote.py --rollback`。只停止匹配自身身份的新版服务并恢复保留的旧二维服务。不会删除或改写用户数据，不改 EDA、引擎、网络、hostname、MAC 或 loopback0。新版三维项目保留在候选 var，旧版不能读取三维时，用新版再次启动打开；不将其改为二维。

工作台 API 不访问外网，服务只监听 loopback。Python Web 进程只读取包元数据，原生 ViennaPS/CUDA 仅在受控 worker 中导入。

GPU 组合在宿主 shell 执行 `./scripts/verify-host-capabilities.sh` 按具体维度、模型、操作、横向边界和引擎逐项核实。该命令更新能力证据并重跑浏览器 GPU 分支；只做候选 8767 的验证，不执行正式迁移。探测使用独立受限子进程和固定种子，不修改驱动、CUDA 或其他系统环境。
