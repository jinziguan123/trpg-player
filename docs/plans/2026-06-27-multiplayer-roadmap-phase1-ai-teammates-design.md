# 多人 roadmap 阶段 1: 开始游戏时可选 AI 队友一起游玩 设计稿

**目标：** 让玩家在开始新游戏时可以选择 1 个主角加若干 AI 队友一起开局，并且 AI 队友会在玩家行动后自动响应。

**架构：** 以 `session_participants` 作为会话参与关系的事实来源，打破现有单席位 `player_character_id` 约束。开局时由前端一次性提交主角与队友席位，后端创建会话和参与记录；运行时沿用现有 `GenerationManager` + SSE 结构，在每次玩家输入后插入一轮“队友自动回合”编排，再把结果交回 KP 生成主叙事。KP 上下文改为读取整个队伍，而不是单一角色。

**技术栈：** FastAPI, SQLAlchemy, Pydantic, React, Zustand, SSE, 现有 LLM / `GenerationManager` / CoC 规则引擎。

---

## 设计原则

1. 先保证可玩，再追求完整战斗回合。
2. AI 队友必须可控，不能无限自触发。
3. 参与关系可扩展，后面接多人真人时不用推倒重来。
4. 保持现有单人游戏路径尽量不受影响。

## 关键决策

- 参与程度选择：A，自主行动。
- 但本阶段只实现“叙事层自动响应 + 简化决策”，不引入完整 initiative / 战斗轮次引擎。
- 每次玩家输入最多触发 1 轮队友自动回合，禁止递归链式自动生成。

## 数据模型

### 现状

- `game_sessions.player_character_id` 只存单个角色。
- `session_service.create_session(db, module_id, player_character_id)` 只接收一个角色。
- `characters.is_player` 已能区分真人 / 非真人。

### 调整方向

- 新增 `session_participants` 表，表示一个会话中的所有席位。
- 每个席位至少包含：
  - `session_id`
  - `character_id`
  - `role`（`human` / `ai`）
  - `seat_order`
  - `is_primary`
  - `joined_at`
- `game_sessions.player_character_id` 先保留为主角快捷字段，便于兼容旧代码与展示。
- 解除“一模组只能有一个进行中 session”的约束，改为只限制同一角色不能同时出现在多个活跃会话里，或按参与表精确校验。

## 会话创建流程

### 前端

- `apps/web/src/pages/GamePage.tsx` 改为：
  - 选择 1 个主角
  - 选择 0 个或多个 AI 队友
  - 队友来源两种：
    - 已有 `is_player=false` 角色
    - 开局前即时 AI 建卡生成后加入
- 提交给后端时携带 `participants`，而不是单个 `player_character_id`。

### 后端

- `POST /api/sessions` 接收参与席位列表。
- 创建会话时：
  - 写入 `game_sessions`
  - 批量写入 `session_participants`
  - 主角写 `human` 且 `is_primary=true`
  - AI 队友写 `ai`
- 保留旧字段的兼容性读法，直到阶段 2 彻底切换。

## 运行时生成链路

### KP 上下文

- `build_kp_context` 不再只收一个 `player_char`。
- 改为收：
  - 主角
  - 所有 AI 队友
  - 当前事件历史
- 系统提示里把“玩家角色信息”扩展为“队伍信息”，让 KP 知道场上有哪些角色在场。

### 玩家输入后的自动队友回合

- 现有 `chat_service.run_chat_generation` 保持主链路。
- 玩家发送消息后，先记录玩家行动，再进入队友自动回合：
  1. 读取当前在场队友
  2. 为每个队友构建上下文
  3. 让队友按顺序产生一次响应意图
  4. 将队友的结果写入事件流
  5. 再把完整事件历史交给 KP 做收束叙述
- 队友输出建议采用结构化 JSON，而不是直接复用 KP 的自然语言标签。

### 防失控

- 每次玩家输入只允许 1 轮队友自动编排。
- 自动回合不能再触发新的自动回合。
- JSON 解析失败时直接 `hold`，不重试递归。
- 限制最大队友数，避免单次上下文和生成成本失控。

## AI 队友策略

- 队友的目标不是“抢戏”，而是“补位与响应”。
- 每个队友只做一次决策，决策结果可以是：
  - 发言
  - 行动
  - 协助
  - 保持沉默
- 队友判断依据：
  - 当前场景
  - 玩家刚刚做了什么
  - 自身性格 / 背景 / 技能
  - KP 允许的信息边界

## 前端游戏页

- `apps/web/src/pages/GameSessionPage.tsx` 增加队伍展示。
- 聊天气泡继续沿用现有样式，但要能区分：
  - 主角
  - 队友
  - NPC
  - KP
- 输入框逻辑不变，仍由玩家主动发出行动。
- 角色面板保留主角展示，队友可通过独立区域查看。

## 兼容性

- 旧单人会话数据继续能读。
- 旧 session 记录如果没有 `session_participants`，可按 `player_character_id` 做回退展示。
- `characters?available=true` 的筛选逻辑需要和新会话参与关系对齐，避免队友被误判为“可用角色”。

## 测试策略

1. 后端单测：创建会话时可写入多个参与者，主角和队友席位标记正确。
2. 后端单测：同一角色不能重复加入活跃会话。
3. 后端单测：玩家输入后只触发 1 轮队友自动回合，不会递归。
4. 后端单测：`build_kp_context` 能把整个队伍写进提示词。
5. 前端回归：GamePage 仍可正常开局，GameSessionPage 仍可正常聊天和订阅流。

## 非目标

- 不做完整战斗轮次系统。
- 不做队友主动被玩家点名指令的交互面板。
- 不做联机真人多人同步。
- 不改现有 CoC 规则引擎的核心判定逻辑。

