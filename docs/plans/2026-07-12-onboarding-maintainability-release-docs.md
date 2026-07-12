# 首团闭环、可维护性与分发文档实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让新用户从首页经 AI 配置引导进入自制示例单人会话，同时恢复前端生产构建、拆分首团路径相关大页面，并补齐开源与内容分发文档。

**Architecture:** 后端新增独立 onboarding 编排服务，在一个数据库事务里确保自制示例模组、预设调查员和单人会话存在；前端新增 `/onboarding` 状态页，未配置 AI 时经路由状态进入设置页，测试成功后返回继续。页面拆分只提取现有业务边界，不改变手动车卡、开房与联机行为。

**Tech Stack:** FastAPI、SQLAlchemy、pytest、React 19、TypeScript 6、React Router 7、Vitest、Testing Library、Vite、pnpm。

---

### Task 1: 恢复前端 TypeScript 构建基线

**Files:**
- Create: `apps/web/src/types/dice-box-threejs.d.ts`
- Create: `apps/web/src/lib/liveState.ts`
- Modify: `apps/web/src/components/game/DiceRoller.tsx:57`
- Modify: `apps/web/src/pages/GameSessionPage.tsx:20,370-400,848`
- Modify: `apps/web/src/pages/CharacterPage.tsx:552`
- Modify: `apps/web/src/pages/ModuleDetailPage.tsx:70-100,210-260`
- Modify: `apps/web/src/pages/ModulePage.tsx:260-290`

**Step 1: 固化失败基线**

Run: `pnpm --filter web exec tsc --noEmit`

Expected: FAIL，稳定出现第三方模块声明、`ref`、SSE metadata、模组结构和 `ReactNode` 类型错误。

**Step 2: 为 SSE 状态边界编写失败的类型守卫测试**

在 `apps/web/src/lib/liveState.test.ts`（Task 2 建立测试框架后执行）覆盖：

```ts
expect(parseCombatState({ round: 1, turn: 0, order: [] })).toEqual({ round: 1, turn: 0, order: [] })
expect(parseCombatState({ round: '1' })).toBeNull()
expect(parseChaseState({ round: 1, gap: 2, escape_at: 6, caught_at: 0, pursuer: {}, target: {} })).not.toBeNull()
```

本 Task 先实现纯函数并由 `tsc` 校验；Task 2 补跑上述行为测试。

**Step 3: 修复真实类型边界**

- 为 `@3d-dice/dice-box-threejs` 声明实际使用的默认构造器签名。
- 将 `forwardRef<DiceRollerHandle, Record<string, never>>` 改为没有字符串索引签名的空属性类型。
- 在 `liveState.ts` 实现 `parseCombatState`、`parsePendingReaction`、`parseChaseState`，GameSessionPage 只接收通过守卫的数据。
- 模组编辑器使用泛型 `<T extends object>` 保留 `Scene`、`NPC`、`Clue` 的具体类型，不再统一强转字典。
- 模组列表用 `String(value ?? '')` 形成可渲染文本；角色武器通过显式序列化函数生成 API payload。
- 删除未使用的 `GiPositionMarker`。

**Step 4: 验证构建恢复**

Run: `pnpm --filter web exec tsc --noEmit`

Expected: PASS。

Run: `pnpm --filter web build`

Expected: PASS，并生成 `apps/web/dist`。

**Step 5: 提交**

```bash
git add apps/web/src
git commit -m "fix: 恢复前端类型契约与生产构建"
```

### Task 2: 建立前端测试基础设施

**Files:**
- Modify: `apps/web/package.json`
- Modify: `pnpm-lock.yaml`
- Create: `apps/web/vitest.config.ts`
- Create: `apps/web/src/test/setup.ts`
- Create: `apps/web/src/lib/liveState.test.ts`

**Step 1: 安装锁定版本的测试依赖**

Run:

```bash
pnpm --filter web add -D vitest jsdom @testing-library/react @testing-library/jest-dom @testing-library/user-event
```

Expected: `package.json` 与 `pnpm-lock.yaml` 更新，不改变运行时依赖。

**Step 2: 添加失败的守卫测试**

将 Task 1 的合法/非法 metadata 用例写入 `liveState.test.ts`。

Run: `pnpm --filter web test -- --run`

Expected: 初次因 `test` 脚本或 Vitest 配置缺失而 FAIL。

**Step 3: 添加最小 Vitest 配置**

```ts
// vitest.config.ts
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] },
})
```

`package.json` 增加 `"test": "vitest"`；setup 导入 `@testing-library/jest-dom/vitest` 并清理 DOM。

**Step 4: 运行测试与静态检查**

Run: `pnpm --filter web test -- --run`

Expected: PASS。

Run: `pnpm --filter web lint`

Expected: exit 0；既有 warning 单独记录，不在本 Task 扩散处理。

**Step 5: 提交**

```bash
git add apps/web/package.json apps/web/vitest.config.ts apps/web/src/test apps/web/src/lib/liveState.test.ts pnpm-lock.yaml
git commit -m "test: 建立前端 Vitest 回归基础"
```

### Task 3: 实现原子的新手团后端编排

**Files:**
- Create: `server/app/content/__init__.py`
- Create: `server/app/content/onboarding.py`
- Create: `server/app/services/onboarding_service.py`
- Modify: `server/app/services/session_service.py:95-168`
- Test: `server/tests/test_onboarding_service.py`

**Step 1: 写失败的服务测试**

覆盖：

```python
def test_start_creates_owned_sample_module_character_and_active_session(db): ...
def test_start_reuses_active_onboarding_session_for_same_token(db): ...
def test_start_rolls_back_everything_when_session_creation_fails(db, monkeypatch): ...
def test_different_tokens_do_not_share_player_character(db): ...
```

断言示例模组的 `world_setting.source == "trpg-player-original"`，角色归当前 token，会话只有一个已就绪真人主角席。

Run: `server/.venv/bin/pytest server/tests/test_onboarding_service.py -q`

Expected: FAIL，因为服务尚不存在。

**Step 2: 定义自制示例内容**

在 `content/onboarding.py` 用 Python 常量提供原创的两场景短模组与完整 CoC 预设调查员数据。使用稳定 slug `first-case-v1`，不要引用现有种子模组、规则书或商业角色。

**Step 3: 将 session 创建拆成可组合事务**

把 `session_service.create_session` 的对象构造抽为不提交的私有函数；公开函数继续保持既有“构造 + commit + refresh”行为。onboarding 服务调用不提交版本，最终统一 `db.commit()`；异常统一 `db.rollback()` 后重抛。

**Step 4: 实现幂等编排**

```python
def start_onboarding(db: Session, token: str) -> tuple[GameSession, bool]:
    # 1. 已有当前 token 的 active/setup 示例局则直接返回
    # 2. 确保原创示例模组存在
    # 3. 创建或复用当前 token 的空闲预设调查员
    # 4. 创建 active 单人会话
    # 5. 单次 commit
```

token 为空必须拒绝，避免不同用户共享匿名角色。

**Step 5: 运行聚焦与全量后端测试**

Run: `server/.venv/bin/pytest server/tests/test_onboarding_service.py server/tests/test_session_participants.py -q`

Expected: PASS。

Run: `server/.venv/bin/pytest -q`

Expected: 全量 PASS。

**Step 6: 提交**

```bash
git add server/app/content server/app/services/onboarding_service.py server/app/services/session_service.py server/tests/test_onboarding_service.py
git commit -m "feat: 原子创建自制示例新手团"
```

### Task 4: 暴露新手团 API 并校验 AI 配置

**Files:**
- Create: `server/app/api/onboarding.py`
- Create: `server/app/schemas/onboarding.py`
- Modify: `server/app/api/router.py`
- Test: `server/tests/test_onboarding_api.py`

**Step 1: 写失败的 API 测试**

覆盖：无 token 返回 401；无激活 AI 配置返回 409 和可行动错误码 `ai_not_configured`；配置可用时返回 `session_id`、`status=active`、`reused`；重复调用返回同一个会话。

Run: `server/.venv/bin/pytest server/tests/test_onboarding_api.py -q`

Expected: FAIL/404。

**Step 2: 添加响应契约与路由**

```python
class OnboardingStartResponse(BaseModel):
    session_id: str
    status: str
    reused: bool
```

端点为 `POST /api/onboarding/start`。先调用 `load_active_profile()` 校验 `api_key` 和 `model_name`，再调用服务；不回传 API Key、角色秘密或模组隐藏内容。

**Step 3: 注册路由并验证**

Run: `server/.venv/bin/pytest server/tests/test_onboarding_api.py -q`

Expected: PASS。

Run: `server/.venv/bin/ruff check server/app server/tests`

Expected: PASS。

**Step 4: 提交**

```bash
git add server/app/api/onboarding.py server/app/api/router.py server/app/schemas/onboarding.py server/tests/test_onboarding_api.py
git commit -m "feat: 提供新手团启动接口"
```

### Task 5: 实现前端首团状态机与 AI 配置返回

**Files:**
- Create: `apps/web/src/features/onboarding/api.ts`
- Create: `apps/web/src/features/onboarding/OnboardingPage.tsx`
- Create: `apps/web/src/features/onboarding/OnboardingPage.test.tsx`
- Create: `apps/web/src/features/onboarding/navigation.ts`
- Create: `apps/web/src/features/onboarding/navigation.test.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/pages/HomePage.tsx`
- Modify: `apps/web/src/pages/SettingsPage.tsx`

**Step 1: 写失败的状态机测试**

使用 MemoryRouter 和 mocked API 覆盖：

- AI 已配置：调用 `/onboarding/start`，导航到 `/game/:id` 并带 `{ isNew: true }`。
- AI 未配置：显示原因和“配置 AI”按钮，点击跳转 `/settings`，state 含 `returnTo: '/onboarding'`。
- 创建失败：保留重试按钮，不重复生成页面级副作用。
- 首页存在唯一的“体验新手团”主入口。

Run: `pnpm --filter web test -- --run src/features/onboarding/OnboardingPage.test.tsx`

Expected: FAIL。

**Step 2: 实现独立 onboarding API 与页面**

页面状态限定为 `checking | needs_config | creating | error`，不把 API Key、profile 或角色数据写入 URL/localStorage。

**Step 3: 接入设置页返回意图**

从 `useLocation().state` 读取并校验 `returnTo` 只允许站内已知路径。`AISettingsPanel` 的“测试成功”回调由 SettingsPage 注入；只有测试结果 `success=true` 时导航回 `/onboarding`。

将返回意图解析放在 `navigation.ts` 纯函数中，测试恶意绝对 URL 会回落到 `null`。

**Step 4: 接入路由与首页入口**

新增 `/onboarding` 路由。首页保留“上传模组”和常规“开始游戏”为次级路径，“体验新手团”为主要操作。

**Step 5: 验证**

Run: `pnpm --filter web test -- --run src/features/onboarding`

Expected: PASS。

Run: `pnpm --filter web exec tsc --noEmit`

Expected: PASS。

**Step 6: 提交**

```bash
git add apps/web/src/App.tsx apps/web/src/pages/HomePage.tsx apps/web/src/pages/SettingsPage.tsx apps/web/src/features/onboarding
git commit -m "feat: 打通首页到 AI 配置的新手团流程"
```

### Task 6: 拆分 GamePage 的现有业务边界

**Files:**
- Create: `apps/web/src/features/game-setup/types.ts`
- Create: `apps/web/src/features/game-setup/moduleFilters.ts`
- Create: `apps/web/src/features/game-setup/moduleFilters.test.ts`
- Create: `apps/web/src/features/game-setup/useGameSetup.ts`
- Create: `apps/web/src/features/game-setup/NewGamePanel.tsx`
- Create: `apps/web/src/features/game-setup/JoinRoomPanel.tsx`
- Create: `apps/web/src/features/game-setup/SessionList.tsx`
- Modify: `apps/web/src/pages/GamePage.tsx`

**Step 1: 为现有筛选行为写特征测试**

测试关键词、年代、地区、难度、人数区间交集和 reset，不先改变算法。

Run: `pnpm --filter web test -- --run src/features/game-setup/moduleFilters.test.ts`

Expected: FAIL，因为模块尚不存在。

**Step 2: 提取纯筛选函数和共享类型**

迁移 `parsePlayerRange`、`filterModules` 和筛选状态类型。让 GamePage 与测试共用同一实现。

**Step 3: 提取 hook 和三个视图组件**

`useGameSetup` 负责加载模组/角色、席位状态、创建会话和 AI 生成角色；组件只接收值与命令。`JoinRoomPanel` 独立处理远端主机地址规范化；`SessionList` 独立处理状态徽标和删除确认。

**Step 4: 验证行为保持**

Run: `pnpm --filter web test -- --run src/features/game-setup`

Expected: PASS。

Run: `pnpm --filter web build`

Expected: PASS。

**Step 5: 提交**

```bash
git add apps/web/src/features/game-setup apps/web/src/pages/GamePage.tsx
git commit -m "refactor: 拆分开房与房间列表边界"
```

### Task 7: 拆分 CharacterPage 的列表与 API 边界

**Files:**
- Create: `apps/web/src/features/characters/api.ts`
- Create: `apps/web/src/features/characters/characterPayload.ts`
- Create: `apps/web/src/features/characters/characterPayload.test.ts`
- Create: `apps/web/src/features/characters/CharacterList.tsx`
- Create: `apps/web/src/features/characters/CharacterList.test.tsx`
- Modify: `apps/web/src/pages/CharacterPage.tsx`
- Modify: `apps/web/src/pages/GamePage.tsx` or `apps/web/src/features/game-setup/useGameSetup.ts`

**Step 1: 为现有角色 payload 和列表行为写失败测试**

覆盖：武器与装备序列化不丢字段；角色搜索匹配姓名、职业和规则；分页边界；选中/编辑/删除事件不互相冒泡。

Run: `pnpm --filter web test -- --run src/features/characters`

Expected: FAIL。

**Step 2: 提取 API 与序列化函数**

集中 `createCharacter`、`generateCharacter`、`listAvailableCharacters`；`characterPayload.ts` 接收结构化 `WeaponItem[]` 并显式输出后端 JSON 契约。

**Step 3: 提取 CharacterList**

迁移搜索、分页与卡片展示；CharacterPage 保留车卡向导状态和详情面板。不得改变草稿、Excel 导入、AI 生成和七步流程。

**Step 4: 复用 API 并验证**

Game setup 的 AI 席位生成改用同一 character API，删除重复请求拼装。

Run: `pnpm --filter web test -- --run src/features/characters src/features/game-setup`

Expected: PASS。

Run: `pnpm --filter web build`

Expected: PASS。

**Step 5: 提交**

```bash
git add apps/web/src/features/characters apps/web/src/pages/CharacterPage.tsx apps/web/src/features/game-setup
git commit -m "refactor: 拆分角色列表与创建契约"
```

### Task 8: 补齐许可证、内容声明和 README

**Files:**
- Create: `LICENSE`
- Create: `CONTENT_NOTICE.md`
- Modify: `README.md`
- Replace: `apps/web/README.md`
- Modify: `docs/packaging.md`

**Step 1: 写文档验收清单**

在提交前逐项确认 README 包含：一句话定位、桌面端首选启动方式、已实现/实验性/规划中、首团入口、AI 配置、可信局域网边界、测试命令和公开分发阻断项。

**Step 2: 添加 Apache-2.0 代码许可证**

根 `LICENSE` 使用标准 Apache License 2.0 全文。`CONTENT_NOTICE.md` 明确：代码许可证不自动授予第三方规则书、商业模组、用户上传内容、字体和其他素材的版权；公开分发包只能包含原创或已授权内容。

**Step 3: 重写项目文档**

- README 不再把已实现的多人、地图、战斗、追逐和桌面打包列为待开发。
- `apps/web/README.md` 删除 Vite 模板文案，改为前端开发、测试和构建说明。
- `docs/packaging.md` 增加发布前内容审计清单，并明确现有种子内容不得默认视为可公开分发。

**Step 4: 验证链接与命令**

Run: `rg -n "待开发.*地图|待开发.*多人|React \+ TypeScript \+ Vite" README.md apps/web/README.md`

Expected: 无匹配。

Run: `git diff --check`

Expected: PASS。

**Step 5: 提交**

```bash
git add LICENSE CONTENT_NOTICE.md README.md apps/web/README.md docs/packaging.md
git commit -m "docs: 明确功能现状与内容分发边界"
```

### Task 9: 端到端验证与视觉巡检

**Files:**
- Modify only if verification reveals a scoped defect.

**Step 1: 运行后端全量验证**

Run: `server/.venv/bin/ruff check server/app server/tests`

Expected: PASS。

Run: `server/.venv/bin/pytest -q`

Expected: 全量 PASS。

Run: `server/.venv/bin/python -m evals.run --smoke`

Expected: `7/7 通过` 或当前 fixture 总数全部通过。

**Step 2: 运行前端全量验证**

Run: `pnpm --filter web test -- --run`

Expected: PASS。

Run: `pnpm --filter web exec tsc --noEmit`

Expected: PASS。

Run: `pnpm --filter web build`

Expected: PASS。

Run: `pnpm --filter web lint`

Expected: exit 0。

**Step 3: 浏览器验证核心路径**

启动后端与 Vite，使用浏览器依次验证：

1. 首页首团入口在桌面和窄视口均无溢出。
2. 无 AI 配置时进入设置；失败测试不返回。
3. 配置测试成功后返回 onboarding，并进入新会话。
4. 重复点击不会创建重复 active 示例局。
5. 手动开房、角色列表和加入房间仍可达。

检查控制台错误和关键页面截图；不在验证中调用真实付费模型，开场生成以 API mock 或已有测试覆盖。

**Step 4: 检查分支差异与工作区**

Run: `git diff --check master...HEAD`

Expected: PASS。

Run: `git status --short`

Expected: 空。

**Step 5: 最终提交（仅当验证产生必要修复）**

```bash
git add <本轮必要修复文件>
git commit -m "fix: 收口新手团端到端验证问题"
```
