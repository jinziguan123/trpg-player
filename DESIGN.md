# 设计文档（DESIGN）

本文件记录 TRPG Player 中**较重要、跨模块、不易从单个文件读出**的设计决策与工作机制，
描述的是「当前实现的事实」。探索期的过程稿、备选方案与阶段规划见 [`docs/plans/`](docs/plans/)。

## 目录

- [KP 回合三段式：规划器（TurnPlan）与校验器（TurnValidator）](#kp-回合三段式规划器turnplan与校验器turnvalidator)
- [长局上下文：滚动剧情摘要](#长局上下文滚动剧情摘要)
- [重新生成：回滚并重跑最新一轮 KP](#重新生成回滚并重跑最新一轮-kp)

---

## KP 回合三段式：规划器（TurnPlan）与校验器（TurnValidator）

> 代码：`server/app/ai/turn_planner.py`、`server/app/ai/turn_validator.py`、
> 接入点在 `server/app/services/chat_service.py`。
> 原始设计稿：[`docs/plans/2026-07-01-kp-turn-planner-design.md`](docs/plans/2026-07-01-kp-turn-planner-design.md)。

### 背景与动机

主叙事由 `KPAgent` 一次生成，同时承担「演 KP 讲故事」和「裁定规则」两件事。单次自然语言
生成里这两类职责互相抢注意力，导致高频问题：**该检定的不检定、把 KP 专属线索/秘密泄露给
玩家、输出机械的「汇报体」（`【标题】`+项目符号列表）而非叙事**。

继续往 `KP_SYSTEM_PROMPT` 堆规则只会让注意力更分散（且该提示词有硬 token 预算）。因此把
KP 回合拆成三步——用两个**低温辅助 LLM 调用**把主叙事夹在中间：

```
玩家行动
  │
  ├─①  TurnPlan       低温结构化 JSON：判断「这一轮该怎么裁定」
  │        │          → 作为一条 system 消息（内部工作稿）注入 KP 上下文
  ├─②  KP 工具循环     KPAgent 流式叙事；工具调用由规则/状态服务执行并回注结果
  │        │
  └─③  TurnValidator  落库前廉价安检：违反计划硬约束则改写「落库版本」
           │
        持久化(EventLog) → 计划状态守卫（如确保开战）→ done
```

规划与校验调用 **fail-open**：失败时退回原始 KP 生成，不阻塞跑团；但规划器一旦成功给出
`combat.should_start=true`，战斗状态切换由后端确定性保证，不再因主叙事模型漏调工具而降级。

### 阶段一：TurnPlan（先裁定）

- **触发**：`_run_generation` 中，**仅非开场**（`events` 非空）才跑；开场或规划失败则不注入，
  KP 走原逻辑。分头/单场景都先跑一次，**整回合共用同一份计划**。
- **输入**（`build_turn_plan_messages`）：把运行时资料压成紧凑 JSON——当前场景、玩家/队友
  精简卡、最近 8 条事件、以及**可见范围内**的 NPC 与线索（含 `trigger_condition`）。两条关键约束：
  - 与 `build_kp_context` 用**同一套** `_active_flags` / `_resolve_state` 把场景/NPC 解析到
    「当前样貌」，避免规划器看到的画像与 KP 实际收到的不一致；
  - 遵守可见场景边界（`_visible_scene_ids`），不把玩家尚未到达区域的线索提前喂进去。
- **调用**（`run_turn_planner`）：`temperature=0` + `response_format={"type":"json_object"}`——
  要的是稳定判断而非创作。
- **输出**：Pydantic 强校验的 `TurnPlan`：

  | 字段 | 含义 |
  |------|------|
  | `turn_kind` | 本轮类型：`investigate / social / move / combat / knowledge / roleplay / mixed`（默认 `mixed`） |
  | `player_intent` | 玩家本轮想达成什么 |
  | `requires_check` + `check` | 是否需要检定；技能 / 难度 / 可见性（明暗投）/ 理由 |
  | `clue_policy` | 行动是否匹配线索、候选线索 id、揭示程度、是否需先灵感 |
  | `npc_policy` | 谁开口、反应基调、是否触发 NPC 行动 |
  | `scene_policy` | 是否切场景、set / clear 哪些 flag |
  | `combat` | 是否立即进入结构化战斗、参战敌方名字和开战原因 |
  | `narration_brief` | 叙事要点清单 |
  | `safety` | `do_not_reveal`（硬隐藏信息）、`do_not_control_players` |

- **注入**（`build_turn_plan_message`）：把计划打成一条 system 消息追加到 KP 上下文，消息本身
  带强约束——「这是内部工作稿，别念给玩家 / 不许复述字段名或内部 id / **不许改用汇报体** /
  `requires_check` 就只描述尝试并发起检定 / `combat.should_start` 就必须调用 `start_combat` /
  `do_not_reveal` 不许泄露」。
- **fail-open**：JSON 解析失败或调用异常 → 返回 `None` → 不注入、KP 走原流程。

### 阶段二：KP 叙事（中间）

单场景主路径在 AI 配置启用工具调用且供应商支持时进入 `_run_kp_agent_loop`：模型的文本增量
继续实时广播；`dice_check`、`san_check`、`say`、`start_combat`、`scene_change` 等标准工具调用
由 `_build_kp_tool_executor` 分发给规则或状态服务，执行结果以 `role=tool` 回注模型，再由模型决定
续写或收束。需要真人掷骰、进入战斗/追逐等工具会返回 `suspend`，立即结束本轮自由叙事。

工具模式关闭或供应商不支持时，系统保留 `_stream_narration_filtered` + `_process_commands` 的文本
指令兼容路径。两条路径共享规划器、校验器、持久化、世界记忆和最终状态守卫；**规则引擎与状态
服务始终是最终执行者**，自然语言本身不直接改变规则状态。

### 战斗切换的确定性保证

`start_combat` 不能只靠主叙事模型“记得调用”。玩家已经明确攻击时，若 KP 只写了冲突描写却
漏掉工具调用，页面只会收到旁白而收不到 `combat_start`，表现为叙事结束后仍停在普通回合。

当前机制把识别与执行分开：

1. `TurnPlan.combat` 结构化给出 `should_start`、`enemies`、`trigger`。规划器只在双方已经进入
   会造成伤害的即时敌对交锋时置为 `true`；威胁、戒备、瞄准或尚未接敌不会误开战。
2. 明确的攻击/射击/格斗宣言会绕过前置的“普通技能检定申请”分诊，确保进入 TurnPlan；该规则
   只决定路由，不凭关键词直接创建战斗，最终仍由规划器结合场景判断。
3. KP 正常调用 `start_combat` 时，工具执行器立即创建战斗态、广播先攻与当前行动者并挂起叙事。
4. 叙事和文本指令处理结束后，`_ensure_planned_combat` 检查计划与持久化战斗态：计划要求开战
   但尚无活动战斗时，由后端补执行 `_exec_start_combat`；已有战斗则幂等跳过，不会重复开场。
5. 该守卫同时接在单场景工具路径、单场景文本兼容路径和分头叙事收尾，避免模型能力或开关差异
   改变核心状态机行为。

这里仍保持职责边界：模型/规划器负责语义判断“是否开战、敌人是谁”，后端只保证已经作出的
结构化裁定必然落地；不会用关键词正则从一段叙事里猜测战斗。

### 阶段三：TurnValidator（落库前安检）

- **触发**：叙事流跑完、**落库前**，`_validate_and_patch_narration`；单场景与分头每列各校验一次。
- **零成本预筛**（`_looks_suspicious`）：并非每轮都调 LLM。只有满足其一才值得付这次调用——
  (a) `safety.do_not_reveal` 非空（有硬隐藏信息，泄露代价高）；(b) 文本已出现「汇报体」正则
  特征；(c) 出现 `flag_xxx` 这类内部标识。都不满足则直接放行。
- **LLM 校验**（`build_validator_messages`）：让安检模型判断这段旁白是否 ① 泄露 `do_not_reveal`
  （即便转述/暗示）② 汇报体 ③ 出现内部 id / 字段名，返回 `{violated, reason, corrected_narration}`。
- **命中违规**：用 `corrected_narration` **替换落库文本**（`result[0]`），并 `del result[3:]`——
  改写后原文的「对话交错偏移」已失真，落库改走「整段旁白 + 对话追加」的回退路径，保证对话不丢。
- **关键局限**：只改**落库版本**。已经流式推给当时在线玩家的那一瞬收不回；但重连、其他玩家、
  事后复盘看到的都是干净版。
- **fail-open**：无 LLM / 解析失败 / 异常一律放行原文；判定违规却没给改写 → 兜底用原文，绝不清空。

### 接入点：哪些生成路径走三段式

三段式**只接在常规玩家输入的主链路**（`run_chat_generation` → `_run_generation` /
`_run_split_generation`）。其余走 `_run_kp_turn` 的路径**刻意不跑** planner/validator，保持简单：

| 生成路径 | 入口 | Planner | Validator |
|----------|------|:-------:|:---------:|
| 常规玩家输入（单场景） | `_run_generation` | ✓ | ✓ |
| 常规玩家输入（分头分栏） | `_run_split_generation` | ✓（每列注入同一份） | ✓（每列各校验） |
| 玩家显式申请检定 / 意图分诊命中 | `_run_kp_turn` ← `run_check_request_generation` | ✗ | ✗ |
| 投骰后续写 | `_run_kp_turn` ← `run_roll_generation` | ✗ | ✗ |
| 大地图前往（travel） | `_run_kp_turn` ← `run_travel_generation` | ✗ | ✗ |
| 开场 | `_run_generation`（`events` 为空） | ✗ | ✗ |

> 检定意图仍由独立的轻量分诊 `_detect_check_request` 处理，未被 planner 取代（降低风险；
> 见设计稿「待确认决策 2」）。

### 分头行动下的行为

队伍身处 ≥2 个场景时逐场景生成（`_run_split_generation`）：整回合仍只跑**一次** planner，
其计划注入每一列；每列**各自**过一遍 validator。另外每列以**自身所在场景**为锚构建 KP 上下文
（`build_kp_context(..., viewer_scene_id=...)`），否则各列都会拿到主角场景的资料、把同一场景
重复叙述一遍。

### 关键取舍

1. **先裁定、再表演**：把「这一轮怎么裁定」先用低温结构化调用定死并作为约束喂给 KP，
   而不是寄望一次自然语言生成同时兼顾表演与裁定。
2. **不直接上完整多 agent**：改动集中、可测试、可回退；每轮只多一次规划调用（validator 靠
   预筛通常跳过）。按职责拆 Rules/Clue/NPC/Narrator 子 planner 留作后续演进。
3. **生成增强 fail-open，核心状态 fail-closed**：规划/校验失败不阻塞叙事；但规划器一旦明确裁定
   开战，后端必须保证战斗态落地，不能让模型漏调工具造成状态机悬空。
4. **Validator 只补落库版本**：承认「已流式内容收不回」，用最低成本保证持久化记录干净。

### 相关代码与测试

- 规划器：`server/app/ai/turn_planner.py`（`TurnPlan` / `build_turn_plan_messages` /
  `run_turn_planner` / `build_turn_plan_message`）
- 校验器：`server/app/ai/turn_validator.py`（`_looks_suspicious` / `validate_turn_narration`）
- 接入：`server/app/services/chat_service.py`（`_run_generation` / `_run_split_generation` /
  `_run_kp_agent_loop` / `_ensure_planned_combat` / `_validate_and_patch_narration`）
- 测试：`server/tests/test_turn_planner.py`、`server/tests/test_turn_validator.py`、
  `server/tests/test_kp_tool_loop.py`、`server/tests/test_chat_service.py`

---

## 长局上下文：滚动剧情摘要

> 代码：`server/app/ai/story_summarizer.py`、`build_kp_context`（`server/app/ai/context.py`）、
> `_maybe_roll_story_summary`（`server/app/services/chat_service.py`）。

**问题**：KP 上下文按 token 预算装配「最近事件全文 + 更早事件的即时摘要」。游戏一长，
即时摘要（`_summarize_old_events`）只能粗暴截断老事件，KP 逐渐「失忆」中段与近段剧情、
原地打转、复读开场式内容。KP 叙事的 LLM 调用本身**不设 max_tokens 上限**，所以这不是
输出长度问题，而是上下文装配问题。

**机制**：维护一份**持久滚动摘要**，随游戏增量更新：

- `game_session.world_state.story_summary`：截至某点的剧情梗概；
  `story_summary_seq`：已并入摘要的最后一条事件 `sequence_num`（游标，默认 0）。
- **何时滚动**：每轮 KP 生成收尾（`done` 之后）调用 `_maybe_roll_story_summary`。当「游标之后
  未并入摘要的事件」超过 `STORY_SUMMARY_TRIGGER(24)` 时，把其中除最近 `STORY_SUMMARY_KEEP_RECENT(12)`
  条以外的较老事件，连同既往摘要交给 `story_summarizer.summarize_story` 低温浓缩成新摘要，
  推进游标。不够阈值则零成本返回，不额外调用 LLM。
- **如何使用**：`build_kp_context` 只把「游标之后」的事件按预算给全文，`story_summary` 作为
  `[之前发生的剧情摘要]` 注入；游标之前的老事件不再逐条进上下文。游标默认 0 → 与旧行为一致。

**fail-open**：摘要生成失败（无 LLM / 异常 / 空）保持原摘要与原游标，绝不阻塞跑团。

---

## 重新生成：回滚并重跑最新一轮 KP

> 代码：`generation_manager.cancel`、`session_service.rollback_last_kp_output`、
> `run_regenerate_generation`、`POST /{session}/regenerate`；前端在 `GameSessionPage.tsx`。

**问题**：生成到一半断网时，KP 侧会卡住（僵死 task 占着并发锁、`done` 永不来），且断流时
落库的半截叙事会污染下一轮上下文。

**流程**（高风险，前端红色二次确认后才触发，仅作用于最新一轮）：

1. `generation_manager.cancel`：取消并等待卡住/进行中的生成 task 真正结束（其半截叙事会先落库）。
2. `rollback_last_kp_output`：删除「最后一条玩家方输入之后」的 KP 叙事产物——旁白、NPC 台词、
   待投骰的检定请求（并清对应 `pending_checks`）；**保留**玩家/队友输入与已投出的骰子结果。
3. `run_regenerate_generation`：用清理后的事件流只重跑 KP（不重跑队友回合、不做检定意图分诊）。
4. 前端点确认即进入「KP 思考中」并 `resyncHistory`——旧叙事从界面立即消失，随后 `/live`
   流式推入重生成内容。

**决策**（经确认）：仅重跑 KP、保留玩家+队友输入；保留已定骰子不重掷；任意时候可对最新一轮重来。

**局限**：回滚只清叙事文本与待投骰请求，**不逆转** HP/场景切换/剧情 flag 等状态变更。
