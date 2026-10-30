# 模型协作与飞书中继框架

通用的单机多模型任务调度工具。主控通过飞书发送结构化任务，云端 Bridge 按 Worker ID 调用本机模型并回复结果。工具不依赖任何业务项目或数据集。

## 架构

```text
主控 → 飞书任务消息 → cloud-desktop-1 Bridge
                         ├─ worker_a → 模型 A，独立上下文
                         ├─ worker_b → 模型 B，独立上下文
                         └─ kimi_code → Kimi Code ACP（适配器待实现）
```

- 一台机器运行一个 Bridge，模型 Worker 不分别加入飞书群。
- `target_bridge` 决定接收机器，`assigned_worker` 决定本机 Worker。
- 每个 Worker 只收到自己的任务 payload，不共享其他 Worker 上下文。
- 飞书只负责传输与状态展示，SQLite 和 JSON 产物是事实来源。
- 不通过飞书发送凭证或大型文件，只发送受控引用、任务说明和摘要。

## 状态与结果

- 状态：`pending → running → succeeded／blocked／failed`。
- 结果字段：`status、summary、artifacts、unknowns、blocking_issues`。
- 飞书 `event_id` 防止事件重推，`idempotency_key` 防止任务重复执行。
- API Key 只从环境变量读取，不写入任务记录。
- 运行产物默认写入 `model_orchestration/runtime/`，不提交到 Git。

## 中继消息契约

```json
{
  "protocol": "model-relay-v1",
  "kind": "task",
  "task_id": "TASK-example-001",
  "idempotency_key": "example-task-v1",
  "target_bridge": "cloud-desktop-1",
  "assigned_worker": "mock",
  "task_type": "generic",
  "role": "smoke_tester",
  "input_ref": "controlled-reference",
  "instructions": "Return a mock success result"
}
```

Bridge 返回 `kind: "result"`，增加 `bridge_id`、`worker_id`、`error` 和五个结果字段。普通群聊、结果消息和发往其他 Bridge 的消息会被忽略，避免机器人循环回复。

## 测试

在工具仓根目录执行：

```bash
PYTHONPATH=model_orchestration python3 -m unittest \
  discover -s model_orchestration/tests -v
```

## 本地任务闭环

```bash
PYTHONPATH=model_orchestration python3 -m orchestrator.cli create \
  --type generic \
  --role smoke_tester \
  --input-ref controlled-reference \
  --instructions 'Return a mock success result' \
  --worker mock
```

使用返回的 `task_id`：

```bash
PYTHONPATH=model_orchestration python3 -m orchestrator.cli run TASK_ID --worker mock
PYTHONPATH=model_orchestration python3 -m orchestrator.cli show TASK_ID
PYTHONPATH=model_orchestration python3 -m orchestrator.cli list
```

## 飞书 Mock Bridge

前置条件：

1. 飞书企业自建应用已启用机器人，配置接收群消息和机器人发消息权限。
2. 应用已订阅 `im.message.receive_v1`，使用长连接，机器人已加入目标群。
3. 云端安装并配置 `lark-cli`，`lark-cli auth status` 显示 Bot `ready`。
4. 获取目标群 `chat_id` 和主控用户的 `open_id`。

启动命令：

```bash
PYTHONPATH=model_orchestration python3 -m orchestrator.lark_bridge \
  --bridge-id cloud-desktop-1 \
  --chat-id oc_xxx \
  --sender-open-id ou_xxx \
  --worker mock \
  --timeout 10m
```

然后在指定群发送纯文本 JSON 任务。Bridge 只处理白名单群、白名单发送者、文本消息及匹配的 `target_bridge`。联调时使用 `--timeout` 或 `--max-events` 做有界运行，不留无界后台进程。

## 接入真实 Worker

OpenAI 兼容 Worker：

```bash
export WORKER_BASE_URL='https://your-endpoint/v1'
export WORKER_MODEL='your-model'
export WORKER_API_KEY='...'
PYTHONPATH=model_orchestration python3 -m orchestrator.cli run TASK_ID \
  --worker openai --base-url "$WORKER_BASE_URL" --model "$WORKER_MODEL"
```

Kimi Code Worker 计划通过 `kimi acp` 接入，每个任务创建独立会话并映射同一结果契约。当前尚未实现真实 ACP 适配器，Mock 联调成功不代表已经能控制 Kimi Code。
