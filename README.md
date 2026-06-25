# TRPG Player

AI 驱动的桌面角色扮演游戏（TRPG）平台，让单个玩家也能快速开启一场跑团。

AI 扮演守秘人（KP）和 NPC，玩家专注于角色扮演。当前支持 CoC（克苏鲁的呼唤）七版规则。

## 核心功能

- **模组上传与 AI 解析**：上传模组文本文件，AI 自动提取场景、NPC、线索等结构化数据
- **CoC 车卡系统**：属性掷骰（三选一）、20+ 职业选择、职业/兴趣技能加点、背景故事
- **AI 文字跑团**：KP Agent 流式叙述 + NPC Agent 独立对话，SSE 实时推送
- **骰子检定**：CoC 七版检定规则，AI 叙述与骰子结果严格一致（先掷骰后叙述结果）
- **信息隔离**：EventLog 的 visibility 机制确保 NPC 只知道应该知道的信息
- **角色卡面板**：游戏界面右侧实时展示角色属性（九维雷达图）、技能、道具

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 + TypeScript + Vite 8 + Tailwind CSS 4 + Zustand 5 |
| 后端 | Python 3.10+ + FastAPI + SQLAlchemy + Alembic |
| 数据库 | SQLite（本地文件，零配置） |
| AI | DeepSeek API（OpenAI 兼容格式），可通过 LLMProvider 抽象切换 |
| 包管理 | pnpm workspace monorepo |

## 项目结构

```
trpg-player/
├── apps/web/                    # React 前端
│   └── src/
│       ├── api/                 # API client + SSE 流式
│       ├── stores/              # Zustand 状态管理
│       ├── components/          # 布局、角色卡、UI 组件
│       └── pages/               # 首页、模组、角色、游戏
├── packages/shared/             # 前后端共享类型
├── server/                      # Python 后端
│   └── app/
│       ├── api/                 # FastAPI 路由
│       ├── services/            # 业务逻辑（ChatService 为核心协调器）
│       ├── ai/                  # AI Agent 系统（KP/NPC Agent + ContextManager）
│       ├── rules/               # 规则引擎（CoC 检定、车卡、职业）
│       ├── models/              # SQLAlchemy ORM 模型
│       └── schemas/             # Pydantic 请求/响应模型
├── package.json                 # pnpm workspace root
└── pnpm-workspace.yaml
```

## 快速启动

### 前置条件

- Node.js >= 18 + pnpm
- Python >= 3.10
- DeepSeek API Key（[获取地址](https://platform.deepseek.com)）

### 1. 克隆并安装依赖

```bash
git clone <repo-url> && cd trpg-player

# 前端依赖
pnpm install

# 后端依赖
cd server
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

### 2. 配置环境变量

```bash
# 在 server/ 目录下创建 .env
cp ../.env.example server/.env
# 编辑 server/.env，填入你的 DeepSeek API Key
```

`.env` 文件内容：

```
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

### 3. 初始化数据库

```bash
cd server
alembic upgrade head
```

> 如果没有 alembic 迁移版本，首次运行时 SQLAlchemy 会自动创建 `trpg.db`。

### 4. 启动开发服务

**方式一**：分别启动

```bash
# 终端 1 - 后端（server/ 目录下）
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# 终端 2 - 前端（项目根目录）
pnpm dev:web
```

**方式二**：一键启动（需安装 concurrently）

```bash
# 项目根目录
pnpm dev
```

前端默认运行在 `http://localhost:5173`，后端运行在 `http://localhost:8000`。
Vite 已配置代理，前端 `/api/*` 请求自动转发到后端。

### 5. 开始使用

1. 打开浏览器访问 `http://localhost:5173`
2. **上传模组**：进入「模组」页面，上传 .txt 或 .md 格式的模组文件
3. **创建角色**：进入「角色」页面，按向导完成车卡（属性→职业→技能→背景）
4. **开始游戏**：进入「游戏」页面，选择模组和角色，开始冒险

## 架构要点

### AI Agent 协作流程

```
玩家输入 → ChatService 记录 EventLog
         → KP Agent 流式叙述（不预测检定结果）
         → 解析 [DICE_CHECK] 指令 → 规则引擎掷骰
         → KP Agent 根据实际骰子结果续写叙述
         → 解析 [NPC_ACT] 指令 → NPC Agent 回应
         → SSE 推送所有结果到前端
```

### EventLog 信息隔离

所有游戏事件记录在 `event_log` 表中，`visibility` 字段存储可见角色 ID 列表。
ContextManager 在构建 AI 上下文时根据 visibility 过滤事件，确保 NPC 只能看到自己参与的信息。

### 规则引擎插件化

`RuleEngine` 抽象基类定义了检定、伤害、车卡等接口，CoC 作为第一个实现。
通过 `registry.py` 注册/获取引擎，扩展 DnD 等规则只需实现新的引擎类。

## 开发说明

```bash
# TypeScript 类型检查
cd apps/web && npx tsc --noEmit

# Python 后端测试
cd server && .venv/bin/pytest

# 格式化等工具请参考各子项目配置
```

## 当前状态

P0 里程碑已完成核心流程：模组上传 → AI 解析 → 车卡 → 文字跑团。

待开发功能包括：地图编辑与渲染、DnD 规则支持、多人游戏、BGM 系统、Tauri 桌面端打包等。
