# TRPG Player Web

React 前端负责模组、角色、房间、跑团和设置界面，通过 `/api` 与 FastAPI 后端通信。开发环境由 Vite 代理请求；桌面包中由后端同源托管 `dist`。

## 开发

在仓库根目录安装依赖并启动：

```bash
pnpm install
pnpm dev:web
```

默认地址为 `http://localhost:5173`。需要同时运行后端时，在另一个终端执行 `pnpm dev:server`，或直接使用根目录的 `pnpm dev`。

## 验证

```bash
pnpm --filter web exec vitest run
pnpm --filter web exec tsc --noEmit
pnpm --filter web build
pnpm --filter web lint
```

测试使用 Vitest、jsdom 和 Testing Library。`build` 同时执行 TypeScript 项目构建与 Vite 生产打包，产物位于 `apps/web/dist`。

## 目录边界

- `src/pages`：页面编排和仍需保留的页面级流程；
- `src/features`：onboarding、开房和角色等独立业务边界；
- `src/components`：跨页面 UI 与领域组件；
- `src/stores`：Zustand 会话和模组状态；
- `src/api/client.ts`：主机地址、玩家 token、JSON 请求和 SSE。

前端不会把 AI Key、配置详情或角色秘密写入 URL。玩家 token 仅用于本地或可信局域网内的轻量身份区分，不是互联网安全鉴权。
