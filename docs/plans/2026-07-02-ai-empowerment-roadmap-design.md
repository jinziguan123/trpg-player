# AI 赋能路线：五个方向的设计稿

**背景**：当前架构是「TurnPlanner → KP 单次流式生成 → TurnValidator」的编排式流水线，
长程记忆只有滚动摘要，实体状态靠 flags 与上下文推断。本设计稿针对五个已确认的方向
给出落地方案：世界记忆层、KP agent loop、导演层、模组原文 RAG、战略方向
（副驾模式 / 战后资产 / 评估回路）。

**总原则**（与现有架构哲学对齐）：
1. fail-open：任何新增 AI 调用失败都不阻塞跑团，回退到现状行为。
2. 确定性优先：能用代码算出来的状态不交给 LLM 猜（与「先掷骰后叙述」同理）。
3. 每个方案独立可关（feature flag 或按数据是否存在自然降级），互不阻塞。

---

## 方案一：世界记忆层（WorldMemory）

### 目标

把「玩家知道了什么」「NPC 记得什么」从上下文推断变成持久化结构，
并让世界在幕后按 NPC 的动机演进。

### 1.1 数据模型

不建新表，扩展 `game_sessions.world_state`（JSON），与 `flags` / `visited_scenes` 同层。
生成由 `GenerationManager` 串行化，无并发写入问题：

```jsonc
{
  "clue_ledger": {
    // 只记录已被触碰的线索；未触碰 = 不在字典里
    "clue_basement_key": {
      "status": "known",          // partial（有所察觉）| known（完全掌握）
      "discovered_by": ["char_a"], // 谁知道（分头行动下信息不共享）
      "seq": 142,                  // 发现时的事件序号
      "note": "在书桌暗格找到，尚未意识到对应地下室"  // 一句话备注
    }
  },
  "npc_memory": {
    "npc_butler": {
      "attitude": "warming",       // hostile|wary|neutral|warming|trusting
      "attitude_reason": "玩家替他隐瞒了偷酒的事",
      "promises": ["答应半夜带玩家看西厢房"],
      "lies_told": ["谎称老爷死时自己在厨房"],
      "interactions": [            // 环形缓冲，最多保留 8 条
        {"seq": 130, "summary": "被 char_a 用心理学看穿慌张"}
      ]
    }
  },
  "backstage": {
    "last_run_seq": 140            // 幕后推演游标
  }
}
```

### 1.2 更新机制：确定性来源优先，抽取器兜底

**v1（纯确定性，零额外 LLM 调用）**，在 `_process_commands` 与落库路径中挂钩：

| 来源 | 更新动作 |
|---|---|
| `TurnPlan.clue_policy`：`reveal_level != none` 且有 `candidate_clue_ids` | 台账写入 partial/known（partial ← hint，known ← direct），`discovered_by` 取本轮在场角色 |
| `[NPC_ACT]` 执行后 | 该 NPC `interactions` 追加一条（trigger 原文截断即可，不调 LLM） |
| `[SAY: who=]` 落库时 | 同上，记录「对谁说了话」 |
| 暗投心理学等 blind check 命中 NPC | `interactions` 追加「被看穿/未被看穿」 |

**v2（MemoryKeeper 抽取器，低温兜底）**：每轮 KP 生成完毕后、与
`_maybe_roll_story_summary` 同一位置追加一次 `complete()`（temperature=0，
`response_format=json_object`）。输入 = 本轮新事件 + 当前 npc_memory 摘要，
输出 = 差量：

```json
{"npc_updates": {"npc_butler": {"attitude": "wary", "attitude_reason": "...",
  "new_promises": [], "new_lies": []}}, "clue_notes": {}}
```

只允许改 attitude/promises/lies/note 字段，台账的 status 变更仍以确定性来源为准
（防止抽取器幻觉出「玩家已知」）。失败静默跳过。
可与摘要合并为一次调用（同一输入，输出多一个字段）以省成本——推荐先做合并版。

### 1.3 注入点

- `build_kp_context`（context.py）：
  - 新增「线索台账」小节：已 known/partial 的线索列表（谁、何时、备注），
    并明确指示「以下线索玩家已经掌握，不要重复安排发现桥段；
    未列出的线索一律视为未发现」。这同时解决「KP 重复喂线索」的老毛病。
  - NPC 小节：每个在场 NPC 拼上其 `npc_memory`（态度 + 承诺 + 谎言 + 最近互动）。
- `build_npc_context`：该 NPC 自己的 memory 全量注入——NPC 记得对玩家的承诺和
  说过的谎，这是玩家最能感知的改进。
- `run_turn_planner`：台账作为 `clue_policy` 判断的输入（已发现的不再是 candidate）。
- `story_summarizer`：prompt 调整为「线索与 NPC 关系已有专门台账，摘要侧重剧情脉络，
  不必逐条保留线索细节」，缓解摘要膨胀。

### 1.4 幕后推演（Backstage Clock）

**触发**：每 6 个玩家回合，或 `[SCENE_CHANGE]` 发生时（场景切换是天然的时间流逝点）。
条件不满足直接跳过：模组无带 secrets/goals 的 NPC 则永不触发。

**BackstageAgent**（新增 `ai/agents/backstage_agent.py`）：
- 输入：NPC 的 secrets + 动机、未触发的 triggers、线索台账、当前场景、幕后游标以来的事件摘要。
- 输出（低温 JSON）：0~2 条幕后事件
  `{"npc_id": "...", "action": "把尸体从地窖移到井里", "affected_scene": "scene_well", "suggest_flags": ["flag_body_moved"]}`。
- **安全约束**：v1 幕后事件不直接改 flags（避免 AI 自把自为改变世界与模组脱轨），
  只落库 + 注入 KP 上下文，是否 `[SET_FLAG]` 由 KP 在后续叙事中决定。

**存储**：落 `event_logs`，`visibility=["kp"]`（引入 `"kp"` 哨兵值，
所有玩家侧查询过滤掉含此哨兵的事件），`event_type="system"`，
`metadata={"kind": "backstage"}`。KP 上下文新增小节
「幕后动态（玩家不可见，用于把握世界演进，不要直接复述）」。
Validator 的 `_looks_suspicious` 预筛清单加上幕后事件文本片段。

### 1.5 测试点

- 台账写入：构造带 `clue_policy` 的 plan，断言 world_state 更新（纯单测，不调 LLM）。
- NPC 上下文：npc_memory 存在时 `build_npc_context` 含承诺/谎言文本。
- 幕后事件对玩家不可见：历史接口按玩家 token 查询不返回 `visibility=["kp"]` 事件。

---

## 方案二：KP 从流水线到 Agent Loop（function calling 迁移）

### 目标

方括号指令迁移到标准 tool use；续写 prompt（DICE/RULE 两套 CONTINUATION）
消解为循环内的 tool result；planner 前移为全回合共享契约。

### 2.1 Provider 层扩展

`LLMProvider` 新增（默认实现保持不支持，兼容所有现有 Provider）：

```python
def supports_tools(self) -> bool: return False

async def stream_chat(
    self, messages: list[dict], tools: list[dict] | None = None,
    temperature: float = 0.7, max_tokens: int | None = None,
) -> AsyncIterator[StreamDelta]: ...
```

`StreamDelta` 为 dataclass：`kind: "text" | "tool_call"`，text 带 chunk，
tool_call 带 `{name, arguments, id}`。OpenAI 兼容 Provider（DeepSeek 等）与
Anthropic Provider 各自实现流式 tool_call 聚合。

### 2.2 工具注册表（单一事实来源）

新增 `ai/tools.py`：每个现有指令一条注册项，含 JSON Schema + 中文能力说明 + 执行器引用：

| 工具 | 语义 | 循环行为 |
|---|---|---|
| `dice_check` / `san_check` | 检定 | 执行掷骰，结果作为 tool result 回注，**继续生成**（替代 KP_DICE_CONTINUATION_PROMPT） |
| `rule_lookup` / `module_lookup` | RAG 检索 | 返回 top-3 段落，继续生成（替代 KP_RULE_CONTINUATION_PROMPT） |
| `npc_act` | 触发 NPC | 执行 NPCAgent，台词落库并广播，result 回注 |
| `set_flag`/`clear_flag`/`scene_change`/`move`/`hp_change` | 状态变更 | 执行后返回 "ok"，继续生成 |

**双轨渲染**：`render_tools_as_prompt(tools)` 把同一份注册表渲染成现有方括号指令说明文本。
`supports_tools()` 为假或配置关闭时走旧正则路径——能力文档只维护一份，两条路径共用。

### 2.3 Agent Loop

`chat_service` 新增 `_run_kp_agent_loop`（与 `_stream_narration_filtered` 并列，
按 `ai_settings.use_tool_calls` 开关二选一）：

```
loop (max_steps=6):
    stream_chat(messages, tools)
    text chunk → 现有台词/段落过滤器 → 实时广播
    tool_call → 执行器执行（掷骰/检索/落库）→ append tool result → continue
    自然结束 → break
组装完整叙事 → TurnValidator → 落库
```

- 步数上限 6，超限强制收尾（append「请直接收束本轮叙述」的 system 消息再生成一次）。
- `rule_lookup` 沿用每轮最多 2 次的限制，在执行器层拒绝并返回提示文本。
- Validator 保持终检位置不变；新增**改写回推**：命中违规时，除替换落库版本外，
  向房间广播 `narration_patch` 消息（含事件 id + 新文本），前端就地替换该段。
  这修复「玩家已看到违规流式内容、重连才看到改写版」的不一致。

### 2.4 Planner 前移为共享契约

`run_chat_generation` 编排调整：

```
现状：意图分诊 → 队友回合 → planner → KP
调整：意图分诊 → planner（读玩家输入+全队名册）→ 队友回合（读 plan）→ KP（读 plan + 队友实际行动）
```

- `TurnPlan` 新增 `team_guidance: str = ""`（本轮对 AI 队友的一句话指引，
  如「本轮重点是审讯管家，队友不要开新话题」）与
  `spotlight: list[str] = []`（建议给戏份的角色 id，见方案三）。
- `team_system` prompt 注入 `team_guidance`；队友决策依旧自主，指引是软约束。
- plan 生成于队友行动之前，KP 叙事时以实际事件为准——plan 是「裁定意图」不是「剧本」，
  这个语义现状已经如此，无额外风险。

### 2.5 迁移分期

1. **M1**：Provider 扩展 + 工具注册表 + 双轨渲染（行为无变化，旧路径改读注册表渲染文本）。
2. **M2**：`dice_check` / `rule_lookup` 两个「终止性指令」进 loop（收益最大：消灭两套续写 prompt）。
3. **M3**：其余指令进 loop，旧正则路径保留为降级开关一个大版本后移除。
4. **M4**：planner 前移（独立小步，可先行）。

### 2.6 测试点

- FakeProvider 增加 tool_call 流式桩，断言 loop 的步数上限、rule_lookup 次数限制、
  tool result 注入顺序。
- 双轨一致性：注册表渲染出的方括号说明与现网 prompt 逐段 diff（一次性快照测试）。
- `narration_patch`：validator 命中时房间收到 patch 消息且落库版本一致。

---

## 方案三：导演层（并入 TurnPlanner，不新增调用）

### 目标

节奏经营：聚光灯均衡、卡关检测、伏笔回收。**信号确定性计算，LLM 只负责把信号变成叙事动作**。

### 3.1 确定性信号（纯代码，每轮计算）

新增 `ai/director_signals.py`：

| 信号 | 计算方式 | 阈值 |
|---|---|---|
| `spotlight_starved` | 最近 30 条事件中每个玩家角色作为 actor 或被叙事点名的次数 | 某角色计数为 0 且他人 ≥ 3 |
| `stuck` | 距上次「台账新增 / flag 变更 / 场景切换」的玩家回合数 | ≥ 3 回合 |
| `unresolved_threads` | 未触发的 triggers + 台账中 partial 状态的线索 | 仅罗列，不设阈值 |
| `pacing_hint` | 最近 N 轮的 turn_kind 分布（连续 4 轮 investigate 无 social/combat → 单调） | 连续 4 轮同类 |

### 3.2 注入方式

信号作为一段结构化文本注入 `run_turn_planner` 的输入；`TurnPlan` 新增：

```python
class DirectionPolicy(BaseModel):
    pacing: Literal["hold", "tighten", "release"] = "hold"
    spotlight: list[str] = []      # 本轮应主动给戏份的角色 id
    nudge: str = ""                # 卡关时的推进手段（让某线索更显眼/NPC 主动来找）
    foreshadow: str = ""           # 建议埋设或回收的悬念，一句话
```

KP system 消息里渲染为「导演笔记（内部指引，严禁向玩家复述原文）」。
`spotlight` 同时进 `team_guidance` 生效于队友回合（AI 队友主动把话头递给冷场玩家）。

### 3.3 边界

- 导演只影响「怎么讲」，不改变世界状态；`nudge` 不允许直接判定检定成功。
- 无信号触发且非每 4 轮的整点时，`DirectionPolicy` 保持默认值，planner prompt
  不含导演段落（省 token）。
- 若后续发现 planner 一次调用承载过重（裁定质量下降），再拆独立 DirectorAgent——
  信号计算层不变，只是换个消费者。

### 3.4 测试点

信号计算全部纯函数单测：构造事件序列断言 `spotlight_starved` / `stuck` 的触发与不触发。

---

## 方案四：模组原文 RAG 与 Handouts

### 目标

跑团时能引用模组原文的笔力与细节；手册/信件类内容成为一等公民。
数据源现成：`Module.raw_content` 已留存全文。

### 4.1 数据模型

新表 `module_chunks`，完全镜像 `rule_chunks` 的形态（复用同一套 embedder 与检索代码）：

```python
class ModuleChunk(Base, UUIDMixin):
    __tablename__ = "module_chunks"
    module_id: str  # FK modules.id, ondelete CASCADE, index
    scene_hint: str | None  # 章节归属的场景 id（标题模糊匹配得出），可空
    ordinal: int
    text: str
    embedding: bytes  # float32 BLOB，与 RuleChunk 一致
```

**建索引时机**：模组解析完成后的后台任务（复用规则书的 indexing/ready/failed 状态机，
`modules` 表加 `rag_status` 列）。切块 ~500 字、10% 重叠；切块后用场景标题在块内
模糊匹配回填 `scene_hint`。存量模组通过「重建索引」按钮补建。

### 4.2 检索与注入

`rulebook_service` 泛化出共用的向量检索函数（cosine top-k over BLOB），两处调用：

1. **被动注入**（`build_kp_context`）：query = 当前场景标题 + 玩家本轮输入，
   检索时 `scene_hint == 当前场景` 的块得分乘以加权系数（如 1.3）优先当前场景原文。
   top-3 注入为小节：

   > 模组原文摘录（供叙事风格与细节参考。**其中可能包含玩家尚未触及的内容，
   > 泄密约束照常适用，只取与当前处境相符的部分**）

   泄密风险的三重防线：scene_hint 加权让摘录大概率是当前场景文本；
   planner 的 `safety.do_not_reveal` 同样看得到摘录、可把其中的秘密列入禁区；
   validator 终检兜底。
2. **主动检索**：新指令/工具 `module_lookup(query)`（方案二注册表新增一项），
   与 `rule_lookup` 共享每轮 2 次的配额。

Token 预算：摘录段计入现有 ~6000 token 系统提示预算，摘录单块截断 400 字。

### 4.3 Handouts

- **解析**：`PARSE_PROMPT_TEMPLATE` 增加提取目标
  `handouts: [{id, title, kind(letter|news|diary|note), content, location, trigger_condition}]`，
  要求 content **保留原文**不改写；`modules` 表加 `handouts` JSON 列（alembic）。
- **发放**：新指令/工具 `[HANDOUT: id=...]`。执行器落库
  `event_type="system"`, `metadata={"kind": "handout", "handout_id": ...}`，content 为原文。
- **渲染**：前端按 metadata.kind 渲染成信笺样式卡片，图标用 `GiScrollUnfurled`
  （遵守开发守则：图标由前端按语义渲染，不进内容字符串）。
- KP 上下文的线索/handout 小节列出「可发放的 handout 清单（id + 标题 + 发放条件）」，
  已发放的进台账（`clue_ledger` 同结构，`kind: handout`）。

### 4.4 测试点

- 切块与 scene_hint 回填纯函数单测。
- 检索加权：同 query 下当前场景块排位提升（用固定向量桩，不调真实 embedder）。
- handout 发放幂等：重复发放同 id 只落库一次。

---

## 方案五：战略方向

### 5.1 KP 副驾模式（human KP + AI copilot）——架构草案

**定位判断**：面向现存 KP 群体，对 AI 质量容错高；且方案一~四建成后，
副驾＝把这些 agent 能力从「自动编排」改为「按钮触发」，边际成本低。
**建议排在方案一~四之后**，此处只锁定架构决策防止返工：

- **数据模型**：`session_participants` 的角色扩展出 `role: "player" | "kp"`；
  `game_sessions` 加 `kp_mode: "ai" | "human"`。human 模式下
  `run_chat_generation` 不自动触发 KP 回合。
- **KP 控制台**（前端新页签，仅 KP 席可见）：
  - 悄悄话频道：`visibility=["kp"]` 的事件流（复用方案一的哨兵）。
  - 能力按钮 = 已有服务的直接调用：查规则（rule_lookup）、查模组（module_lookup）、
    让 NPC 说话（NPCAgent，KP 可改后发出）、暗骰、记忆面板（台账/NPC 关系的读写 UI）。
  - **代拟叙述**：调 KPAgent 生成草稿进编辑框，KP 修改后作为 narration 落库——
    AI 产出永远经人手，这是副驾与自动 KP 的本质区别。
- **分期**：M1 = kp 席位 + 悄悄话 + 代拟叙述；M2 = 记忆面板与按钮全家桶。

### 5.2 战后资产（成本最低，可随时插队）

- **战报 recap**：会话 `ended`（或手动「章节小结」）时，summarizer 变体产出结构化
  recap JSON：`{关键抉择, 已解/未解线索(读台账), 名场面引用(带事件 seq), 阵亡与损失}`，
  存 `world_state.recaps[]`，前端时间线展示。
- **团记导出**：`GET /api/sessions/{id}/replay?style=novel|script`。
  实现：事件流按 1500 token 分窗，逐窗低温改写（novel=小说体，script=剧本体），
  窗间携带上一窗结尾保证衔接，拼接为 markdown 下载。纯离线批处理，不影响跑团路径。
- **成长结算**：CoC 成长检定。数据已够——dice 事件的 metadata 含 skill 与 outcome，
  会话内成功过的技能可确定性汇总。规则引擎加 `improvement_check`（d100 > 当前值
  或 > 95 则 +1d10），结算页玩家逐项掷骰，KP Agent 生成一段成长叙述收尾。
  规则逻辑进 `rules/`（插件式，不硬编码 CoC）。

### 5.3 评估回路（建议最先动工的基建）

后续所有方案都要密集改 prompt，没有评估地板会越改越玄学。**建议在方案一之前先建最小版**。

- **目录**：`server/evals/`（与 pytest 分离，跑真模型、花钱、手动触发）。
- **评测集**：`python -m evals.snapshot <session_id> --turn <seq>` 从真实会话导出 fixture
  （截至某轮的事件 + 模组 + world_state + 该轮 plan），脱敏后入库 `evals/fixtures/`。
  初始 10~15 个，覆盖：开场、检定裁定、NPC 对话、分头、暗投、卡关、临近泄密的危险轮。
- **运行器**：`python -m evals.run --suite kp_core`。对每个 fixture 重放
  `build_kp_context → narrate`，产物过两类检查：
  - **确定性检查**（免费）：无内部 id 泄漏（flag_/scene_/npc_ 裸 id）、指令语法合法、
    未替玩家角色行动（正则匹配玩家名 + 动作动词的启发式）。
  - **裁判模型**（rubric 逐项 0/1）：是否泄露 `do_not_reveal` 项、是否遵循 plan 的
    check/clue 政策、是否汇报体、叙事是否衔接上文。
- **产出**：scorecard JSON 落 `evals/results/<date>-<git-sha>.json`，附对比脚本
  diff 两次运行。prompt 改动的 PR 附 scorecard 对比作为惯例。

---

## 实施顺序与依赖

```
P0（先行基建，1 个迭代）
  └── 5.3 评估回路最小版（10 个 fixture + 确定性检查 + 裁判 rubric）

P1（并行，互不依赖）
  ├── 方案一 v1：线索台账 + NPC 记忆（确定性来源版）
  └── 方案四：模组原文 RAG 被动注入 + module_lookup

P2
  ├── 方案三：导演信号 + DirectionPolicy 并入 TurnPlan
  ├── 方案二 M4：planner 前移 + team_guidance（可与方案三同 PR）
  └── 方案一 v2：MemoryKeeper 抽取器（与摘要合并调用）

P3（大工程，独立分支）
  ├── 方案二 M1-M3：tool use 迁移
  ├── 方案一 幕后推演（依赖 visibility=["kp"] 哨兵）
  └── 方案四 Handouts（依赖解析 prompt 改版 + 前端卡片）

P4（战略层）
  ├── 5.2 战后资产（无依赖，可随时提前，适合当节奏调剂）✅ 已落地
  │      战报 recap（recap.py/recap_service.py，world_state.recaps）、
  │      成长结算（RuleEngine.improvement_check + growth_service）、
  │      团记导出（replay.py/replay_service.py，/replay?style=novel|script）
  └── 5.1 KP 副驾模式（依赖方案一/四的服务化能力）— 暂缓
```

> 另：上下文占用预估（context_estimate + /context-estimate + 前端徽标 + AIProfile.context_window）
> 已落地，作为后续「上下文压缩」的基础（不属原 P4，独立小特性）。

**风险备忘**：
- 模组原文注入的泄密风险靠三重防线缓释但非归零，评估集里必须有「摘录含秘密」的危险轮 fixture。
- tool use 迁移期间双轨并存，旧路径移除前双轨快照测试必须常绿。
- world_state JSON 持续膨胀（台账 + 记忆 + recap），单会话上限预估 < 100KB，SQLite 无压力；
  npc_memory.interactions 已设环形上限，recaps 不设限但只在结算时写入。
