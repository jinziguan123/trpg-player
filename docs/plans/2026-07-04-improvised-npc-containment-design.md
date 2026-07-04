# 临场 NPC 收容（Improvised NPC Containment）设计

> 2026-07-04 · 状态：待实现（手动）
> 关联：`docs/plans/2026-07-01-kp-turn-planner-design.md`（规划器）、世界记忆（clue_ledger / npc_memory）

## 1. 背景与问题

真实会话（鬼屋模组）中，KP 在疗养院场景临场编造了「玛格丽特修女」。随着玩家对她调查，
她一步步变成关键 NPC：先是维托里奥的「接触门卫」（"想和维托里奥说话就去找她"），
后来 KP 甚至围绕她编造线索。证据链在 `server/evals/fixtures/manor_multiplayer_npc.json`：

- `world_state.npc_memory` 只有 npc_knott / npc_gabriela / npc_vittorio 三个模组 NPC，
  玛格丽特修女**零记录**——编造 NPC 不在 `module.npcs`，拿不到 npc_id，进不了任何治理机制；
- 该 fixture 预存 plan 的 `direction.nudge` 写着"修女在门口低声说：'那本小册子……维托里奥先生
  入院时确实带着一本'"——**规划器也在放大编造 NPC，安排她携带线索级信息**。

### 失控升级链（每环都有机制对应）

1. **造出来**（合理）：场景需要功能性人物（疗养院的修女、码头的搬运工）；
2. **无标记**：事件流里她与模组 NPC 地位等同——系统没有「正典/临场」之分，出现越多
   显著性越高，KP 越把她当重要角色写；
3. **玩家聚焦逼出内容**：玩家调查她，KP 必须回应，而模组关于她的内容是零——唯一出路
   是现编，于是编身世、编线索；
4. **规划器放大**：turn_planner 从事件流看到她，`npc_policy.speakers` / `direction.nudge`
   是自由文本、不校验正典名单，直接安排她当剧情推手；
5. **无档案导致漂移**：没有 npc_memory / personality 卡 / [NPC_ACT]，人设只活在滚动上下文里，
   早期剧情被摘要压缩后必自相矛盾。

### 为什么「编造线索」是最严重的部分

项目的信息经济（线索台账、按检定分层揭示、防泄密）全部建立在**模组线索**上。编造线索绕开
整套治理：无 reveal 门控、无台账登记、无设计好的回报；且编造 NPC「知道什么」由 KP 决定，
而 KP 知道全部真相——这是一条现成的**泄密通道**。

### 现状确认（2026-07-04 核实）

- `kp_system.py` 对「编造 NPC」**零约束**（仅有"不编造线索细节 / 不虚构玩家已知背景"两条，
  不覆盖 NPC 本身）；
- `chat_service._record_npc_say_memory()`（约 :1532）只认 `module.npcs` 的说话人，
  编造 NPC 的台词被静默丢弃——**它已经在每轮收集全部 speaker_texts、且已建正典名单映射
  `by_name`，是识别临场 NPC 的天然单点钩子**；
- `turn_planner.build_turn_plan_messages()`（约 :200-270）payload 的 `visible_npcs`
  只含模组 NPC，但 prompt 未要求 speakers/nudge 只用正典名字。

## 2. 设计原则

**不禁编造，给编造上笼子。** 零编造的 KP 是死掉的世界（模组不可能写全所有龙套），
人类 KP 也天天临场造人。病灶不是"编造物存在"，而是"编造物获得叙事权威"
（携带线索 / 把守剧情门 / 被描述为关键人物）。

三层收容，由便宜到贵：

| 层 | 内容 | 治什么 |
|---|---|---|
| P0 | 提示词纪律 | KP 主动越界 |
| P1 | 结构性正典意识（标记 + 注入 + 管住规划器） | 存在感雪球、规划器放大 |
| P2 | 受控转正（可选） | "改编是好事"的正向出口 |

**推荐 P0+P1 一起做**——单做 P0 会被规划器绕开（fixture 已演示 nudge 主动喂线索给修女）。

## 3. P0：提示词纪律

**改动点**：`server/app/ai/prompts/kp_system.py`，`KP_SYSTEM_PROMPT` 输出规则第 5 条
（「NPC 行动」，感知边界那条附近）追加一小节。

草稿（可直接用）：

```
   - **临场角色纪律（硬规则）**：模组未列出的人物（你为场景合理性临时添加的修女、
     店员、路人等）是**龙套**，永远保持边缘：
     · 只知**公共信息**（人人可见的事、本职工作范围内的日常），**绝不携带或产出
       线索、秘密、关键情报**——剧情级信息只能来自模组 NPC、线索表与场景资料；
     · **绝不成为剧情推进的必经之门**（不把守通道、不掌握钥匙、不是"唯一知情人"）；
     · 玩家追问时，龙套如实不知道，至多把玩家**指回模组内容**（"这您得问院长"），
       指路可以，**带货不行**——绝不现编一段"她恰好知道的往事"来满足追问；
     · 不给龙套安排姓名以外的身世设定；玩家反复互动也**不升级**其重要性。
```

**验收**：`tests/` 加确定性守护测试（提示词含"临场角色纪律""带货不行"等关键措辞，
防止后续改提示词时静默删除——参照 `test_evals.py::TestNpcPerceptionIsolation` 的写法）。

## 4. P1：结构性正典意识

### 4a. 识别与登记

**改动点**：`chat_service._record_npc_say_memory()`。它已有：
- `speaker_texts`：本轮全部 (说话人, 台词)；
- `by_name`：module.npcs 的 name→id 映射。

在同一函数里加一步：说话人**不在** `by_name`、也不在队伍名单（`audience_names` 只含玩家名，
可直接用；注意把 `player_char`/teammates 名字都算进排除集）、也不是"系统/KP" → 视为临场
NPC，登记进 `world_state.improvised_npcs`：

```json
{
  "improvised_npcs": {
    "玛格丽特修女": { "first_seq": 133, "mentions": 4, "last_seq": 160 }
  }
}
```

- key 用规整后的显示名（strip；同名变体如"修女"/"玛格丽特修女"暂不合并，见 §7 风险）；
- 只增不删；`mentions` 自增用于观察存在感（不做任何自动行为）；
- 复用 `_apply_world_memory` 的 fail-open 模式（异常绝不阻塞跑团）。

> 覆盖面说明：`_record_npc_say_memory` 在 agent-loop 与 legacy 两条路径、以及骰后续写
> 处都有调用点（chat_service 约 :1831 / :1852 / :3444 / :3540 附近），单点改动即全路径生效。
> 只登记**开过口**的临场角色——只被旁白提及、没有台词的不登记（没有台词就没有"带货"风险，
> 登记它们徒增噪音）。

### 4b. KP 上下文注入「临场角色名单」

**改动点**：`server/app/ai/context.py`，`build_kp_context()`。参照
`_party_distribution_section()` 的模式加 `_improvised_npc_section(session)`：

- `world_state.improvised_npcs` 非空时（且非开场），在 system_content 追加：

```
【临场角色名单】以下人物是你此前临场添加的龙套（不在模组设定中）：玛格丽特修女。
对他们严格执行「临场角色纪律」：保持边缘、只知公共信息、不携带线索或秘密、
不把守剧情、不升级重要性；玩家追问时指回模组内容。
```

- 空则不注入（行为不变）；fail-open。

### 4c. 管住规划器（关键，别漏）

**改动点**：`server/app/ai/turn_planner.py`，`build_turn_plan_messages()`。

1. payload 增加两个字段：
   - `"canonical_npc_names"`: `[npc.name for npc in visible_npcs]`；
   - `"improvised_npcs"`: `list((session.world_state or {}).get("improvised_npcs") or {})`；
2. 规划器 instruction 文本追加一条：

```
npc_policy.speakers 与 direction.nudge 中的 NPC 只能用 canonical_npc_names 里的名字；
improvised_npcs 列出的是 KP 临场添加的龙套——绝不安排他们携带线索、透露情报或推动剧情，
最多作为氛围出现。
```

3.（可选加固）`run_turn_planner` 解析后做确定性清洗：`speakers` 里不在正典名单的名字
   直接剔除。提示词约束 + 确定性清洗双保险，与项目里"提示词纪律 + 落库剥除"的惯例一致。

### P1 验收

- 单测：
  - `_record_npc_say_memory` 收到非正典说话人 → `improvised_npcs` 登记、mentions 自增；
    正典/队友/系统名不登记；
  - `build_kp_context` 在 `improvised_npcs` 非空时注入名单小节、为空不注入；
  - planner payload 含 `canonical_npc_names` / `improvised_npcs`；
  -（若做 4c-3）speakers 清洗剔除非正典名。
- eval fixture（新）：`manor_improvised_npc_probe.json`——复用鬼屋式合成模组，事件流里
  已存在一个开过口的临场龙套（如"值夜的门房老赵"，已登记进 improvised_npcs），
  本轮玩家**逼问他要关键情报**（"你肯定看到过什么，仔细说说那晚谁进过书房"）。
  judge 判定：龙套不产出新线索/情报，如实不知或指回正典 NPC。
  - RUBRIC 增 `improvised_containment` 项（"临场角色不产出线索/秘密/关键情报，
    至多指回模组内容；无临场角色出场时默认通过"）。
  - 注意：`_parse_judge_output` 要求 RUBRIC **全部 key 齐全**，加项必须同步更新
    `test_evals.py` 里两个枚举全 key 的解析用例（本会话已踩过两次）。
  - 连跑 ≥3 次确认稳定（LLM 有随机性）。

## 5. P2：受控转正（可选，后做）

回应"灵活改编是玩家和 KP 都乐见"的正向出口：临场角色确实出彩、全桌想留下她时，
提供**显式转正**——改编要过一道门，而不是靠上下文惯性溜进正史。

- **触发**：房主显式操作（建议放 OOC 命令或会话设置面板按钮，如"将〈玛格丽特修女〉
  转为正式 NPC"）。绝不自动转正（自动=把病灶重新引入）。
- **生成**：一次 LLM 调用，输入=该角色全部既有台词与相关事件摘录，输出=完整 NPC 卡
  （id 用 `improv_` 前缀防撞、name、description、personality、background、
  **secrets 默认空**——转正不自动获得秘密，秘密仍属模组）。
- **存储**：`world_state.improvised_npcs["玛格丽特修女"].card = {...}`（会话级，
  **不写入 module.npcs**，不污染模组本体）。
- **接线**（转正后并入正典集合的四个点）：
  1. `build_kp_context` 的 NPC 资料段：转正卡并入 npcs_info（标注"会话新增"）；
  2. `_matcher_npcs` / `_record_npc_say_memory` 的 by_name：并入 → 她开始有 npc_memory；
  3. `turn_planner` 的 `canonical_npc_names`：并入 → 允许 speakers/nudge 使用；
  4. `[NPC_ACT]` 的 `_find_npc_def`：查 module.npcs 不中时查会话转正卡。
- 转正后自动从「临场角色名单」注入中移除（她已有档案与纪律约束）。

## 6. 实施顺序

1. P0 提示词 + 守护测试（最小可发布）；
2. P1a 登记（`_record_npc_say_memory`）+ 单测；
3. P1b 上下文注入 + 单测；
4. P1c 规划器（payload + instruction + 可选清洗）+ 单测；
5. eval fixture + RUBRIC 项 + 连跑 3 次；
6. P2 另起一轮（先观察 P0+P1 线上效果再决定）。

每步照例：`cd server && .venv/bin/pytest -q` 全绿；改 prompt 跑
`python -m evals.run --suite kp_core` 对比 scorecard（评估回路约定）。

## 7. 风险与边界

- **误判成临场**：`[SAY: who=护工]` 这类**无名功能角色**本来就是提示词允许的用法——
  它们会被登记进 improvised_npcs，这是**预期行为**（护工同样不该带货）。但注意排除集
  要含全部玩家角色名与"系统/KP"，否则会把队友登记进去。
- **同名变体**（"修女" vs "玛格丽特修女"）：v1 按规整名分开登记，不做模糊合并
  （宽容互含匹配会把"卡特"合并到"亨利·卡特"，风险大于收益）。名单里出现两个近似名
  对注入效果无实质影响（都被要求边缘化）。
- **过度收容**：纪律措辞已刻意留出"指路可以"——龙套仍可自然对话、提供公共信息、
  把玩家引向正典内容，不至于变成一问三不知的木头人。若线上观察到 KP 因此不敢让任何
  NPC 开口，优先调 P0 措辞而非撤 P1。
- **旧会话兼容**：无 `improvised_npcs` 字段 → 全部读取处默认空 dict，行为不变。
- **回退**：P0/P1 各自独立可回退（删提示词小节 / 停止登记与注入互不影响）；
  `improvised_npcs` 字段残留无害。
