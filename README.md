# PersonalTools

与具体业务项目无关的通用个人工具集合。

## 工具

- `model_orchestration/`：飞书任务中继与多模型 Worker 协作框架。支持单机多 Worker 路由、任务幂等、SQLite 状态、JSON 结果、Mock Worker、OpenAI 兼容 Worker，以及飞书群消息收发。

详细用法见 `model_orchestration/README.md`。长期主控、持久 Task／Session、异步执行和复杂任务调度的目标架构见 `model_orchestration/ARCHITECTURE.md`；其中未实现部分会明确标注，不与当前能力混淆。
