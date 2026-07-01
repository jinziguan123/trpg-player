# KP 回合规划器设计稿

**目标：** 让 KP 在每轮玩家输入后稳定兼顾线索揭示、玩家意图判断、NPC 反应、检定难度与剧情节奏，减少“说漏线索 / 忽略检定 / NPC 不回应 / 难度乱跳 / 替玩家行动”等问题。

**核心判断：** 当前系统已经有 `KPAgent`、`NPCAgent`、`TeamAgent`，但主叙事仍由 KP 一次生成承担过多职责。继续往 `KP_SYSTEM_PROMPT` 里堆规则会让模型注意力更分散。更短路径是新增一个**结构化 KP 回合规划器**：先低温产出本轮裁定计划，再让 KP 只负责把计划写成自然叙事与内部指令。

**技术栈：** FastAPI, SQLAlchemy, 现有 `LLMProvider`, `ChatService`, `build_kp_context`, CoC 规则引擎, EventLog。

---

## 一、现状与问题

当前玩家输入后的主链路大致是：

1. `run_chat_generation` 记录玩家行动后，先用 `_detect_check_request` 做轻量检定意图分诊。
2. 有 AI 队友时，先跑一轮 `TeamAgent`。
3. `_run_generation` / `_run_kp_turn` 构建 KP 上下文。
4. `KPAgent` 流式生成一整段叙事。
5. `_stream_narration_filtered` 抽取 NPC 台词、过滤内部标签。
6. `_process_commands` 再解析骰子、SAN、HP、场景切换、剧情 flag、NPC_ACT 等控制指令。

这个流程的问题不是“没有多 agent”，而是 KP 在一次自然语言输出里同时做：

- 判断玩家到底想调查、移动、社交、战斗、求知还是闲聊。
- 判断是否需要检定、用什么技能、什么难度、明骰还是暗投。
- 判断线索能不能给、给多少、是否应该先灵感或知识检定。
- 扮演 NPC 并保持动机、秘密和信息隔离。
- 控制场景切换、剧情 flag、SAN/HP、地图移动等内部指令。
- 写出有氛围的跑团文本，同时不能替玩家说话或行动。

这些职责互相抢注意力。模型一旦开始“表演”，就容易忘掉裁定；一旦专注裁定，又会牺牲 NPC 反应或叙事节奏。

---

## 二、设计原则

1. **先裁定，再表演。** 关键判断先结构化落地，再交给 KP 生成自然语言。
2. **少改主链路。** 保留现有 SSE、EventLog、骰子指令和后处理机制。
3. **优先解决高频漏项。** 第一阶段聚焦玩家意图、线索揭示、检定难度和 NPC 反应，不重写完整 agent 框架。
4. **规划结果必须可验证。** 规划器输出 JSON，失败可回退旧流程；测试可断言字段，而不是只看自然语言。
5. **多 agent 是演进，不是起点。** 先用一个结构化 planner，稳定后再拆出 Clue/Rules/NPC 子 planner。

---

## 三、推荐方案：KP Turn Planner

新增 `KP Turn Planner`，在 KP 自然语言生成前运行一次低温 JSON 规划。

### 输入

规划器输入应来自现有上下文，但比 KP 叙事上下文更偏“裁定工作台”：

- 当前 session、当前场景、已激活 flags。
- 本轮玩家行动与最近事件摘要。
- 玩家角色与队友简表。
- 当前可见场景的 NPC 简表、NPC 秘密摘要。
- 当前可见线索，必须包含 `trigger_condition`。
- 已发现线索或已处理过的线索状态。
- 当前规则系统与可用技能列表。

### 输出

规划器输出 JSON，建议字段：

```json
{
  "turn_kind": "investigate|social|move|combat|knowledge|roleplay|mixed",
  "player_intent": "玩家本轮想达成什么",
  "requires_check": true,
  "check": {
    "skill": "侦查",
    "difficulty": "normal",
    "visibility": "open",
    "reason": "玩家正在搜索书桌暗格，结果不确定且影响线索获取"
  },
  "clue_policy": {
    "action_matches_clue": true,
    "candidate_clue_ids": ["clue_3"],
    "reveal_level": "none|hint|basic|deep|full",
    "requires_inspiration": false,
    "notes": "检定成功后只给账本夹层，不直接解释背后的凶手身份"
  },
  "npc_policy": {
    "speakers": ["npc_butler"],
    "reaction": "管家警觉，试图转移话题，但不主动坦白秘密",
    "needs_npc_act": false
  },
  "scene_policy": {
    "scene_change": null,
    "set_flags": [],
    "clear_flags": []
  },
  "narration_brief": [
    "描述玩家尝试检查书桌，不写检定结果",
    "让管家短促插话阻拦",
    "以 DICE_CHECK 结束"
  ],
  "safety": {
    "do_not_reveal": ["管家就是纵火者"],
    "do_not_control_players": true
  }
}
```

### 使用方式

`ChatService` 在常规 KP 生成前调用 planner：

1. 构建 planner messages。
2. 低温调用 LLM，要求 `response_format={"type":"json_object"}`。
3. 解析并校验 JSON。
4. 把规划结果作为一条 system/user message 注入 KP 生成：
   - “你必须按此裁定计划生成叙事。”
   - “若 `requires_check=true`，只描述尝试过程，并以指定检定指令收尾。”
   - “不得揭示 `do_not_reveal` 内容。”
5. KP 继续使用现有 `_stream_narration_filtered` 和 `_process_commands`。

---

## 四、为什么不是直接上完整多 agent

### 方案 A：继续加强单 KP 提示词

优点：改动最小。

缺点：当前提示词已经很长，新增规则会继续挤占注意力。问题本质是单次自然语言生成内的职责冲突，不是提示词少了几条规则。

结论：不推荐作为主方向，只适合补极小的边界。

### 方案 B：新增结构化 Turn Planner

优点：改动集中，能先解决最痛的漏项；planner JSON 可测试，可失败回退；不破坏现有流式输出体验。

缺点：每轮多一次 LLM 调用，有延迟和成本；需要设计 schema 与兜底策略。

结论：推荐。

### 方案 C：完整多 agent 编排

把 RulesAgent、ClueAgent、NpcDirector、DifficultyAgent、Narrator 全部拆开。

优点：职责最清晰，长期上限高。

缺点：编排复杂度、延迟、错误恢复和测试成本都明显上升。当前代码的后处理已经很复杂，贸然拆太多会放大维护压力。

结论：作为阶段 2/3 演进，不作为第一步。

---

## 五、数据与上下文调整

### 线索上下文

当前 `_compact_clues` 只给 KP：

- `id`
- `name`
- `description`
- `location`

但模组解析时已经有 `trigger_condition`。这正是判断“玩家有没有摸到线索”的关键字段。设计要求：

- planner 的线索输入必须包含 `trigger_condition`。
- KP 叙事上下文可继续压缩，但 planner 必须看到完整触发条件。
- 后续可新增线索状态，如 `discovered_clue_ids`，避免重复给同一线索。

### 模组解析

`PARSE_PROMPT_TEMPLATE` 已要求抽取：

- scenes 的 `danger` / `atmosphere` / `states`
- npcs 的 `background` / `secrets` / `skills` / `states`
- triggers
- clues 的 `trigger_condition`

第一阶段无需改上传解析结构，只要在运行时使用这些字段。

### 已发现状态

当前共享类型里 `Clue.discovered` 存在，但服务端线索是否已发现没有明确以 session 维度落库。建议短期放在 `game_session.world_state`：

```json
{
  "discovered_clues": ["clue_1", "clue_3"]
}
```

这不是第一版 planner 的硬依赖，但应作为同一阶段的后续增强。否则 planner 能判断“本轮可给什么”，却难以稳定避免重复给。

---

## 六、组件设计

### `turn_planner.py`

新增服务模块，职责：

- `build_turn_plan_context(...)`
- `run_turn_planner(llm, context) -> TurnPlan`
- JSON 解析与基础校验。
- 失败时返回 `None`，由调用方回退旧流程。

### `TurnPlan` schema

可用 Pydantic model 定义：

- `turn_kind`
- `player_intent`
- `requires_check`
- `check`
- `clue_policy`
- `npc_policy`
- `scene_policy`
- `narration_brief`
- `safety`

第一版字段宁可少，也要稳定。不要一次性把所有未来能力塞进去。

### `ChatService` 接入点

主要接入 `_run_generation` 和 `_run_kp_turn`：

- 常规玩家输入：先 planner，再 KP。
- 玩家显式申请检定：可以复用 planner 替代 `_detect_check_request`，也可以第一版保持 `_detect_check_request`，只在普通路径使用 planner。
- 骰子结果续写：第一版不跑 planner，继续用 `KP_DICE_CONTINUATION_PROMPT`；后续可为续写增加 `Result Planner`。

### KP 提示词调整

不要把 planner 的全部规则再复制进 `KP_SYSTEM_PROMPT`。只新增一个短提示：

- “如果收到【本轮裁定计划】，必须服从该计划。”
- “计划中的隐藏信息只用于约束，不得写给玩家。”
- “计划要求检定时，按指定技能、难度、明暗投输出指令。”

---

## 七、运行流程

常规玩家输入后的目标流程：

```text
玩家输入
  -> EventLog 记录
  -> AI 队友回合（如有）
  -> KP Turn Planner 生成结构化计划
  -> KP 根据计划生成叙事/指令
  -> 流式过滤与台词抽取
  -> 指令解析与规则引擎
  -> EventLog / SSE 广播
```

如果 planner 失败：

```text
planner JSON 解析失败 / 调用异常 / schema 不合法
  -> 记录 warning
  -> 回退现有 KP 流程
  -> 不阻塞玩家继续游戏
```

---

## 八、错误处理与兜底

1. **JSON 解析失败：** 回退旧流程，不重试多轮，避免拖慢跑团。
2. **planner 要求不存在的技能：** 尝试映射到角色技能；映射失败则降级为 KP 自行裁定。
3. **planner 要求不存在的线索/NPC/场景：** 忽略对应字段并记录 warning。
4. **planner 与规则冲突：** 规则引擎和现有 `_process_commands` 仍是最终执行者。
5. **KP 不服从计划：** 先靠提示约束；第二阶段增加 `Turn Validator` 检查并可触发一次修正生成。

---

## 九、测试策略

### 单元测试

1. planner context 包含当前场景可见线索，且保留 `trigger_condition`。
2. planner context 不包含未访问场景的线索。
3. planner JSON 能被解析为 `TurnPlan`。
4. planner 失败时 `_run_generation` 回退旧路径。
5. `requires_check=true` 时注入 KP 的计划包含技能、难度、明暗投。

### 集成测试

1. 玩家输入“我搜查书桌暗格”，planner 选择调查类行动，候选线索命中书桌相关线索。
2. 玩家输入“我用心理学看他有没有撒谎”，planner 选择心理学暗投或检定路径，不被普通叙事吞掉。
3. NPC 在场且玩家逼问其秘密时，planner 要求 NPC 反应，但不直接泄露秘密。
4. 玩家只是闲聊时，planner 不强行触发检定。
5. 危险场景中同样动作可提高难度，但不能无理由堆到 extreme。

### 回归测试

继续跑后端测试：

```bash
cd server && .venv/bin/pytest -q
```

如果改到前端展示，再跑：

```bash
cd apps/web && npx tsc --noEmit && npx vite build
```

---

## 十、非目标

- 不重写 `KPAgent`、`NPCAgent`、`TeamAgent` 的基础抽象。
- 不替换现有 `[DICE_CHECK]` / `[SAN_CHECK]` / `[SCENE_CHANGE]` 等指令机制。
- 不在第一版做完整多 agent 并发编排。
- 不引入复杂战斗 initiative 系统。
- 不要求每轮必定多次 LLM 自我修正。

---

## 十一、阶段划分

### 阶段 1：结构化 Turn Planner

- 新增 planner prompt 与 Pydantic schema。
- planner context 包含 `trigger_condition`。
- 常规 KP 生成前注入本轮计划。
- 失败回退旧流程。
- 补关键单测。

### 阶段 2：线索状态与 Validator

- 在 `world_state.discovered_clues` 记录已发现线索。
- `_process_commands` 或 KP 续写后更新线索发现状态。
- 新增轻量 `Turn Validator`，检查 KP 是否违反 planner 的硬约束。

### 阶段 3：按职责拆 agent

在阶段 1/2 稳定后，再拆：

- `RulesPlanner`：专管检定、难度、明暗投。
- `CluePlanner`：专管线索候选、揭示层级、已发现状态。
- `NpcDirector`：专管 NPC 是否出声、态度和动机。
- `Narrator`：专管自然语言叙事。

---

## 十二、待确认决策

1. 第一阶段是否允许每轮多一次 LLM 调用？建议允许，因为这是换取稳定裁定的主要成本。
2. planner 第一版是否替换 `_detect_check_request`？建议先不替换，普通路径接入 planner；显式检定申请路径保持现状，降低风险。
3. 线索发现状态是否同步纳入第一阶段？建议第一阶段先让 planner 看到 `trigger_condition`，第二阶段再做 session 级 `discovered_clues`。
4. 是否接受 planner 失败静默回退旧流程？建议接受，并记录 warning，避免跑团中断。
