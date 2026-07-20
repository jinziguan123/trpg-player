# ADR-006：OpenAPI 生成与兼容策略

- 状态：已接受
- 日期：2026-07-20

## 背景

前端此前主要依赖页面内手写 TypeScript interface 和 `api.get<T>()` 泛型，`packages/shared` 未被实际导入，且字段命名与后端 Schema 已发生漂移。FastAPI 已能生成 REST OpenAPI 3.1，但部分接口返回裸 `dict`、动态 metadata 或 SSE 流。

## 决策

1. FastAPI `app.openapi()` 生成的 OpenAPI 3.1 文档是 REST 契约的事实来源，固定导出到 `server/openapi.json`。
2. 使用 `openapi-typescript` 生成 `apps/web/src/api/generated.ts`，生成命令为 `pnpm api:generate`。
3. 保持后端当前 snake_case，不在生成阶段隐式转换字段命名。
4. SSE `/live`、流式 chunk、动态 metadata 和未声明 `response_model` 的匿名响应暂时属于手写协议边界；后续通过补充 Pydantic response model 逐步收紧。
5. CI 必须重新导出 OpenAPI 与 TypeScript 类型并执行 diff 门禁；破坏性 API 变更必须同步迁移、版本或兼容层。
6. 删除未被业务代码引用且已漂移的 `packages/shared`，不再维护第二套手写共享类型。

## 后果

- REST 契约可以被审查、生成和做变更 diff，前端不再新增第三套共享类型。
- 当前生成类型并不等于所有 API 都是强类型，动态响应和 SSE 仍需要手写 DTO；补齐 `response_model` 是后续增量纪律。
- OpenAPI 导出依赖后端 Python 环境，前端类型生成依赖 Node 工具，CI 需要分别安装两类依赖。
