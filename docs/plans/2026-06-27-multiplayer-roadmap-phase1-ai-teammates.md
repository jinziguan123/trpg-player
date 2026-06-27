# 多人 roadmap 阶段 1：AI 队友自动行动 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让开始游戏支持 1 个主角加若干 AI 队友一起开局，并在玩家行动后自动触发队友响应。

**Architecture:** 以 `session_participants` 作为会话席位的事实来源，`game_sessions.player_character_id` 只作为兼容性的主角快捷字段。开局页一次性提交主角和队友席位；聊天链路保持现有 `GenerationManager` + SSE，不改流式基础设施，只在玩家回合后插入一次有上限的“队友自动回合”编排。KP 上下文改为读取整队信息，前端则负责选择席位和展示队伍状态。

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, React, Zustand, SSE, pytest, TypeScript, Vite。

---

### Task 1: 会话席位表与兼容 API

**Files:**
- Create: `server/app/models/session_participant.py`
- Create: `server/alembic/versions/20260627_session_participants.py`
- Create: `server/tests/test_session_participants.py`
- Modify: `server/app/models/session.py`
- Modify: `server/app/models/__init__.py`
- Modify: `server/app/schemas/session.py`
- Modify: `server/app/services/session_service.py`
- Modify: `server/app/api/sessions.py`
- Modify: `server/app/api/characters.py`

**Step 1: Write the failing test**

先写一个回归测试，覆盖两件事：
1. `create_session` 可以接收 `participants` 列表并写入 1 个主角 + N 个 AI 队友。
2. 活跃会话里的角色不能重复加入新会话。
3. `/api/characters?available=true&is_player=false` 能只返回可选 AI 角色。

```python
def test_create_session_with_participants_and_ai_filter(db_factory):
    ...
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd server && .venv/bin/pytest tests/test_session_participants.py -v
```

Expected: FAIL，因为当前 `create_session` 还只收单个 `player_character_id`，也没有 `session_participants`。

**Step 3: Write minimal implementation**

补齐最小可用实现：
1. 新增 `SessionParticipant` 模型与迁移。
2. `SessionRead` / `SessionCreate` 增加参与席位结构。
3. `session_service.create_session` 改为接收席位列表，内部写入会话与参与关系。
4. `list_sessions` / `get_session` 继续兼容旧字段，但优先读参与表。
5. `list_characters` 增加 `is_player` 过滤。

**Step 4: Run test to verify it passes**

Run:
```bash
cd server && .venv/bin/pytest tests/test_session_participants.py tests/test_chat_service.py -v
```

Expected: PASS。

**Step 5: Commit**

```bash
git add server/app/models/session_participant.py server/alembic/versions/20260627_session_participants.py server/app/models/session.py server/app/models/__init__.py server/app/schemas/session.py server/app/services/session_service.py server/app/api/sessions.py server/app/api/characters.py server/tests/test_session_participants.py
git commit -m "feat: 支持多人会话席位"
```

---

### Task 2: 玩家回合后的 AI 队友自动响应

**Files:**
- Create: `server/app/ai/agents/team_agent.py`
- Create: `server/app/ai/prompts/team_system.py`
- Create: `server/tests/test_ai_teammates.py`
- Modify: `server/app/ai/context.py`
- Modify: `server/app/services/chat_service.py`
- Modify: `server/app/services/session_service.py`

**Step 1: Write the failing test**

先写一个只验证编排边界的测试：
1. 玩家发言后会触发一次队友自动回合。
2. 每个队友只响应一次。
3. 队友不会把自己再递归触发成下一轮自动回合。
4. `build_kp_context` 能看到整个队伍，而不是只看到主角。

```python
def test_team_turn_runs_once_and_stops(monkeypatch, db_factory):
    ...
```

**Step 2: Run test to verify it fails**

Run:
```bash
cd server && .venv/bin/pytest tests/test_ai_teammates.py -v
```

Expected: FAIL，因为当前没有队友自动编排器，也没有队伍上下文输入。

**Step 3: Write minimal implementation**

补最小闭环：
1. 新增队友 agent 和 prompt，让它输出结构化 JSON 意图。
2. `chat_service` 在玩家输入后插入一轮受限的队友响应。
3. 队友响应只允许一次，不再反向触发新回合。
4. `build_kp_context` 改成读取主角 + 队友列表。
5. 继续沿用 `GenerationManager`，不重做 SSE 基础设施。

**Step 4: Run test to verify it passes**

Run:
```bash
cd server && .venv/bin/pytest tests/test_ai_teammates.py tests/test_chat_service.py -v
```

Expected: PASS。

**Step 5: Commit**

```bash
git add server/app/ai/agents/team_agent.py server/app/ai/prompts/team_system.py server/app/ai/context.py server/app/services/chat_service.py server/app/services/session_service.py server/tests/test_ai_teammates.py
git commit -m "feat: 添加 AI 队友自动响应"
```

---

### Task 3: 开局页多席位选择与游戏页队伍展示

**Files:**
- Modify: `apps/web/src/pages/GamePage.tsx`
- Modify: `apps/web/src/pages/GameSessionPage.tsx`
- Modify: `apps/web/src/stores/sessionStore.ts`
- Modify: `apps/web/src/api/client.ts`（如需要补请求体/返回类型）
- Create: `apps/web/src/components/game/PartyRoster.tsx`（如果页面内复用需要）

**Step 1: Write the failing test / check**

前端没有现成测试框架，就用 TypeScript 类型检查作为第一道门：
1. 先把 `sessionStore` 的创建参数和会话返回类型改成席位列表的预期形状。
2. 让 `GamePage` 和 `GameSessionPage` 暂时引用新字段，触发类型报错。

**Step 2: Run check to verify it fails**

Run:
```bash
cd apps/web && npx tsc --noEmit
```

Expected: FAIL，因为当前还没有 `participants`、队友选择状态和队伍展示字段。

**Step 3: Write minimal implementation**

补齐前端闭环：
1. `GamePage` 改成主角 + AI 队友多选，并支持从现有 `is_player=false` 角色里挑选。
2. 如需“现造”队友，复用现有 `characters/ai-generate` 流程补一个轻量入口。
3. `sessionStore.createSession` 改为提交席位数组。
4. `GameSessionPage` 从会话数据里读参与者，展示队伍和主角。
5. 聊天区区分主角、队友、NPC 和 KP。

**Step 4: Run check to verify it passes**

Run:
```bash
cd apps/web && npx tsc --noEmit && pnpm build
```

Expected: PASS。

**Step 5: Commit**

```bash
git add apps/web/src/pages/GamePage.tsx apps/web/src/pages/GameSessionPage.tsx apps/web/src/stores/sessionStore.ts apps/web/src/api/client.ts apps/web/src/components/game/PartyRoster.tsx
git commit -m "feat: 开局页支持 AI 队友选择"
```

---

### Task 4: 端到端冒烟与回归

**Files:**
- Modify: 仅在前 3 个任务里落下的相关文件

**Step 1: Run backend regression**

Run:
```bash
cd server && .venv/bin/pytest -v
```

Expected: PASS，至少要覆盖会话、聊天、AI 队友和角色筛选相关测试。

**Step 2: Run frontend regression**

Run:
```bash
cd apps/web && npx tsc --noEmit && pnpm build
```

Expected: PASS。

**Step 3: 手工冒烟**

在本地跑一次完整路径：
1. 进入开始游戏页。
2. 选择主角和 1 个 AI 队友。
3. 开局后发一条行动。
4. 确认队友响应只出现一次，KP 继续收束叙事。

**Step 4: Commit**

```bash
git add server apps/web
git commit -m "feat: 完成多人阶段一冒烟回归"
```

