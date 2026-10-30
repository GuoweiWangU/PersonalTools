# 长期主控与多模型协作工具

通用的单机多模型任务调度工具，不依赖任何业务项目或数据集。Mac 主控可以通过飞书群把复杂工作交给云电脑；云端 Bridge 负责持久化、模型调用、受控执行、异步通知和结果验收。

## 能力概览

- `/glm`、`/kimi`：独立、一次性分析任务；`/kimi` 保持只读。
- `/agent`、`/continue`：Kimi Code 通用执行，同一 Task 固定一个 ACP Session，多轮 Turn 恢复上下文。
- `/goal`：持久 Goal／Plan／Step DAG，按依赖自动调度后续步骤。
- 异步队列、心跳、取消、权限审批、资源锁、审计日志和重启恢复。
- Artifact Manifest 独立检查真实文件，记录路径、大小和 SHA256。
- SQLite 是状态事实来源，飞书只承担交互与通知。

## 飞书命令

```text
/agent 检查环境并生成报告
/continue TASK-xxx 基于上次结果继续检查依赖
/status TASK-xxx
/logs TASK-xxx
/tasks
/cancel TASK-xxx
/close TASK-xxx
/approve TASK-xxx
/deny TASK-xxx 不允许该操作

/goal 准备数据并生成质量报告
/glm 独立复核这个结论
/kimi 只读分析当前目录
/mock 测试链路
/help
```

`/agent` 立即返回 Task 和 Turn ID，不等待工作完成。执行完成、失败、阻塞或等待审批时，Bridge 会再次回复原消息。继续任务时必须带原 Task ID；不会隐式新建 Session。

`/close` 只关闭没有活动 Turn 的 Task。运行中的 Task 先 `/cancel`，不要直接关闭。

## Goal DAG

自然语言 `/goal` 创建一个单步骤目标。复杂计划使用 JSON：

```text
/goal {
  "title": "小规模训练与评估",
  "steps": [
    {
      "id": "prepare",
      "instructions": "检查环境并准备小型数据，输出 manifest",
      "depends_on": [],
      "expected_artifacts": ["/absolute/path/data-manifest.json"]
    },
    {
      "id": "train",
      "instructions": "启动分钟级训练并保存检查点和指标",
      "depends_on": ["prepare"],
      "resources": ["gpu:h20-dev1:0"],
      "expected_artifacts": ["/absolute/path/metrics.json"]
    },
    {
      "id": "evaluate",
      "instructions": "使用固定配置评估并生成报告",
      "depends_on": ["train"],
      "expected_artifacts": ["/absolute/path/evaluation.json"]
    }
  ]
}
```

规则：

- `depends_on` 引用同计划中的步骤 ID；循环依赖会被拒绝。
- 前置步骤成功且产物验收通过后，才调度后续步骤。
- `expected_artifacts` 必须是绝对路径；Worker 必须实际生成并在结果中声明这些文件。
- `resources` 是互斥锁名称，可按机器、GPU、数据目录等约定命名。同一资源同时只分配给一个 Task。
- Worker 总结不算独立验收；缺失、越界或不可读的产物会使 Turn／Goal 阻塞。

## 权限边界

- 只有白名单群和白名单发送者可以创建或控制任务。
- `/agent` 文件与终端能力限制在 `--agent-root` 授权的目录集合内。
- SSH 目标必须在 `--ssh-host` 白名单中；密钥由云端本机管理，不进入飞书、SQLite、日志或 Git。
- 普通读取和授权目录内操作可自动通过；高风险删除、系统修改、Git 推送等进入显式审批。
- 不支持或无法识别的工具默认拒绝；审批只选择 `allow_once`，不自动授予永久权限。
- ACP 策略检查不是操作系统沙箱。云端运行账号本身的 OS 权限仍需按最小权限配置，高风险自动化应先小范围验证。

### 受控 MCP 执行器

当前 Kimi Code 的内置 Bash 在 ACP 模式下可能只返回执行成功提示而不返回 stdout，且命令副作用未可靠落盘。因此 `/agent` 的 shell 命令统一使用 `controlled_exec`，分钟级任务使用 `controlled_start`／`controlled_run_status`；`controlled_read`／`controlled_write` 用于文件落盘和回读验证。`/kimi` 只读短任务不挂载这些执行工具。

MCP 执行器复用授权目录、命令与 SSH 白名单策略，记录真实子进程 PID、退出码和输出；长任务日志位于运行数据库旁的 `controlled-runs/`。每次运行绑定实际 Task／Turn，`/cancel` 会终止对应进程组并保留 `cancelled` 状态。首版 MCP 对高风险命令直接拒绝，不进入飞书审批；ACP 原生工具仍按上述审批规则处理。执行器不是操作系统沙箱。

## 云端安装与升级

前置条件：云端已配置 `lark-cli` Bot、Kimi Code 登录和 GLM 环境变量；飞书应用已启用群消息事件。

在工具仓根目录执行：

```bash
CHAT_ID=oc_xxx SENDER_OPEN_ID=ou_xxx \
AGENT_ROOTS="/absolute/project:/data0/allowed:/data3/allowed" \
SSH_HOSTS="mentx-dev0,mentx-dev1,h20-dev0,h20-dev1" \
bash model_orchestration/deploy/install_user_service.sh
```

`AGENT_ROOTS` 使用冒号分隔，默认只有工具仓；`SSH_HOSTS` 使用逗号分隔，默认不允许 SSH。不要把整个系统盘或包含凭证的目录不必要地加入授权根。

日常运维：

```bash
systemctl --user status personal-tools-relay.service
systemctl --user restart personal-tools-relay.service
journalctl --user -u personal-tools-relay.service -n 100 --no-pager
```

代码升级：

```bash
git pull --ff-only
PYTHONPATH=model_orchestration python3 -m unittest \
  discover -s model_orchestration/tests -v
systemctl --user restart personal-tools-relay.service
```

升级不会清空 SQLite。重启时：

- queued Turn 自动恢复调度。
- 正在执行的 ACP Turn 标记为 blocked，保留原 Session，要求显式 `/continue`，不伪造成功。
- 外部进程记录先按 PID 对账为 `detached_running` 或 `exit_unverified`，不会盲目重复启动。
- 资源锁和未完成审批会安全复位。

## 本地测试

```bash
PYTHONPATH=model_orchestration python3 -m unittest \
  discover -s model_orchestration/tests -v
python3 -m compileall -q model_orchestration/orchestrator
git diff --check
```

测试覆盖 GLM 代码块解析、ACP `session/new`／`session/load`、权限请求、终端和文件反向 RPC、异步队列、飞书通知、幂等、审批、取消、重启恢复、Goal DAG、资源锁和 Artifact 校验。

## 兼容旧协议

原有一次性 JSON `model-relay-v1` 任务仍可用，`target_bridge` 选择机器，`assigned_worker` 选择 `mock`、`kimi_code` 或 `glm`。普通群聊、结果消息、其他 Bridge 的任务和非白名单发送者会被忽略。

运行产物默认在 `model_orchestration/runtime/`，不提交 Git。API Key 只从环境读取。详细设计见 `ARCHITECTURE.md`。
