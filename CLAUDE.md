# 开发守则（TRPG Player）

本文件由 Claude Code 每次会话自动加载，是本项目的硬性约定。**违反即视为错误**。

## 图标（最高优先级）
- **一切图标都不得使用 emoji / 颜文字 / 符号字符**（🔒🎲💔⚠️☠️📖✓→… 等一律禁止），
  无论前端 JSX 还是后端落库的文本内容。
- 统一使用**矢量图标库**，优先 **game-icons 风格**（`react-icons/gi`，如 `GiReturnArrow`、
  `GiPadlock`、`GiRollingDices`、`GiScrollUnfurled`），与「返回」按钮左侧图标同一风格；
  通用线性图标可用 `lucide-react`。
- 后端不要把图标塞进消息内容字符串；图标一律由前端按消息类型/语义渲染。
- 常用映射参考：线索/秘密（KP 专属）→ `GiPadlock`；骰子 → `GiRollingDices`；
  身份（玩家/AI/房主）→ 见 `components/game/SeatIcon.tsx`。

## 技术栈与架构
- 前端 React + TypeScript + Vite + Tailwind；后端 FastAPI + SQLAlchemy + SQLite。
- 规则相关逻辑走插件式 `RuleEngine`，不硬编码；AI 调用走 Provider 抽象。
- 玩家角色一视同仁，无「主角」特权（房主角色只是默认锚点）。

## 提交与验证
- 所有回复、git commit 信息用**中文**；commit 末尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 改后端跑 `cd server && .venv/bin/pytest -q`；改前端跑 `cd apps/web && npx tsc --noEmit && npx vite build`。
- 新行为补单测，别破坏既有断言。
