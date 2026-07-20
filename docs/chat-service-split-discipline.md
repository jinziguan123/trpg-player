# `chat_service` 增量拆分纪律

`server/app/services/chat_service.py` 是当前跑团主链路的事实编排器，约 5,000 行，连接了 API 输入、回合状态、LLM、工具执行、规则服务、事件落库、SSE 广播和后台收尾。它不能通过一次性重构解决；后续拆分必须以小步、可回滚、可验证为纪律。

## 已完成的第一刀

“按广播偏移重排回合事件”已经提取到：

```text
server/app/services/turn_event_order.py
```

`chat_service._reorder_turn_events()` 保留兼容包装，因此现有调用点和测试不需要一次性迁移。该模块只负责事件序号重排，不拥有生成锁、广播或规则执行。

## 拆分原则

1. 每次只提取一个职责簇，不同时改 API、事件协议、提示词和数据结构。
2. 新模块必须有清晰输入输出和单一副作用边界；优先提取纯函数、事件编排、协议解析和持久化适配器。
3. 旧符号先保留兼容包装，至少经过一个完整迭代周期后再删除；禁止一次性全仓替换内部函数名。
4. 新模块不得反向依赖 `chat_service`；公共依赖下沉到领域服务或端口模块。
5. 任何改变事件顺序、可见性、广播时机、重试和幂等语义的拆分，都必须先增加回归测试，再移动代码。

## 推荐拆分顺序

```text
第一批：turn_event_order / event_writer / narration_protocol
第二批：tool_executor / turn_validator_adapter / context_input_builder
第三批：turn_orchestrator / generation_lifecycle / housekeeping
最后：将 API 层从 chat_service 的内部函数调用迁移到 application service
```

## 每一刀的验收门槛

```bash
server/.venv/bin/ruff check server/app server/tests
server/.venv/bin/pytest -q
pnpm --filter web exec tsc --noEmit
pnpm --filter web build
```

服务端拆分至少要覆盖：

- 事件顺序与唯一序号；
- SSE 事件 id/sequence 幂等；
- 生成取消、重试和重新生成；
- KP-only 可见性；
- 工具副作用只执行一次；
- `world_state` 写入版本与并发边界。

## 禁止事项

- 不以“文件行数变少”作为拆分完成标准。
- 不在拆分 PR 中顺便更换 LLM provider、重写 prompt 或引入 Redis。
- 不把跨模块全局字典复制到新文件来伪造边界。
- 不删除旧包装和测试，只为让新模块看起来更干净。

## 完成定义

只有当一个职责簇拥有独立模块、独立测试、明确的事务/广播边界，并且 `chat_service` 不再掌握该职责的内部细节时，才算完成该簇拆分。长期目标是让 `chat_service` 退化为回合用例编排器，而不是继续承载所有实现。
