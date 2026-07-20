# TRPG Player

TRPG Player 是一个本地优先的 AI 跑团桌面应用：AI 担任主持人和 NPC，玩家可独自开团，也可在可信局域网内邀请其他玩家加入。

> 当前项目适合本地桌面使用、开发测试和可信局域网联机。它没有面向公网部署所需的账号体系、强身份认证、TLS 终止、权限隔离和抗滥用措施，请勿直接暴露到互联网。

## 功能状态

### 已实现

- 首页一键体验原创示例团《雾港失灯事件》，自动准备预设调查员；缺少 AI 配置时先进入设置，连接测试成功后返回继续；
- 模组导入与 AI 结构化解析，支持文本、PDF、Word 和常见图片格式，图片解析需要视觉模型；
- CoC 七版完整车卡向导、掷骰/自定义属性、职业与技能、Excel 导入、AI 生成、草稿和角色编辑；
- AI 主持、NPC/队友协作、SSE 流式叙述、事件历史、滚动剧情摘要、上下文预算与规则书/模组 RAG；
- 标准工具调用驱动的 CoC 检定、理智、场景状态和 NPC 行动，并保留文本指令兼容路径；
- CoC 结构化战斗、追逐、已知地点调查板和分头行动；规划器确认开战后，即使 KP 漏调工具，后端也会确定性进入战斗轮；
- 多席位房间、房间码、等待大厅、真人认领与可信局域网主机连接；
- Tauri 桌面外壳、PyInstaller 后端 sidecar、SQLite 本地数据和 macOS/Windows 构建流程。

### 实验性

- AI 模组解析、开场和长局叙事质量受所选模型、上下文窗口与供应商兼容性影响；
- 工具调用模式、战斗子代理、规则书检索和图片解析仍需按模型验证；
- DnD 可作为模组/角色数据类型选择，但完整规则引擎和游戏流程尚未实现；
- 桌面构建尚未提供正式签名、公证、自动更新或经过内容审计的公开安装包；
- 局域网身份使用本地生成的 `X-Player-Token`，只能用于可信网络中的轻量席位归属。

### 规划中

- 完整 DnD 规则支持；
- 可安全部署到非可信网络的账号、授权和传输安全；
- 签名发布、自动更新、可复现构建和持续的跨平台安装测试；
- BGM 与更完整的音频体验。

## 首选使用方式

日常游玩优先使用桌面构建，前后端同源运行，数据保存在系统应用数据目录。当前仓库尚未提供可公开下载的审计安装包，需要在本机完成构建：

```bash
pnpm install
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[packaging]"
cd ..
pnpm desktop:build
```

桌面构建需要 Node.js 20.19+、pnpm、Python 3.10+ 和 Rust。macOS/Windows 的产物位置、平台限制和排查方法见 [桌面打包文档](docs/packaging.md)。公开分发前必须先完成该文档中的内容审计。

首次进入应用后：

1. 打开“设置 → AI 配置”，新增并激活 OpenAI 兼容或 Anthropic 配置；
2. 点击“测试”，确认连接成功；
3. 回到首页点击“体验新手团”，或进入“开始游戏”使用自己的模组和角色。

## 开发启动

安装前端和后端开发依赖：

```bash
pnpm install
cd server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
cd ..
```

同时启动 FastAPI 和 Vite：

```bash
pnpm dev
```

也可以分别运行 `pnpm dev:server` 与 `pnpm dev:web`。前端默认地址为 `http://localhost:5173`，后端为 `http://localhost:8000`，Vite 会代理 `/api` 请求。

AI 配置优先通过应用设置页管理，存放在本地数据目录旁的 `ai_settings.json`。不要提交真实 API Key。

## 验证

```bash
# 前端
pnpm --filter web exec vitest run
pnpm --filter web exec tsc --noEmit
pnpm --filter web build
pnpm --filter web lint

# 后端
server/.venv/bin/ruff check server/app server/tests
server/.venv/bin/pytest -q
cd server && .venv/bin/python -m evals.run --smoke
```

## 技术架构

| 层 | 选型 |
|---|---|
| 前端 | React 19、TypeScript 6、Vite 8、Tailwind CSS 4、Zustand 5 |
| 后端 | Python 3.10+、FastAPI、SQLAlchemy、Alembic |
| 数据 | SQLite、本地素材目录、fastembed RAG |
| 桌面 | Tauri 2、PyInstaller onedir sidecar |
| AI | OpenAI 兼容协议与 Anthropic 协议，可配置模型与上下文窗口 |

核心数据流为：

```text
玩家动作 → TurnPlan 裁定 → KP 工具循环/规则引擎/子代理 → 持久化事件 → SSE → 游戏界面
```

主要目录：

```text
apps/web/          React 前端
server/app/        FastAPI、服务、AI、规则和数据模型
server/tests/      后端测试
server/evals/      叙事与指令评估
src-tauri/         桌面外壳
server/openapi.json  REST OpenAPI 契约真源
docs/              架构决策、设计、实施和打包文档
```

## 许可证与内容边界

项目代码采用 [Apache License 2.0](LICENSE)。这不自动授权规则书、商业模组、用户上传内容、字体、模型权重或其他第三方素材。详细范围和公开分发要求见 [内容与素材声明](CONTENT_NOTICE.md)。

仓库中的开发数据库、种子目录或缓存不应被默认视为可公开分发内容。发布者必须逐项完成来源和许可证审计，只把原创或已明确授权的内容放入安装包。
