Original prompt: 根据/Users/jinziguan/Desktop/trpg-player/docs/plans/2026-07-23-sandbox-enrich-terrain-plan.md文档进行开发

## 2026-07-23

- 已读取实施文档，按任务三、任务二、任务一的顺序开发；每项独立提交。
- 工作区原有未跟踪目录 `output/`，与本任务无关，不触碰。
- 当前会话未提供 codebase-memory-mcp 图谱工具，代码发现退回本地检索。
- 已实现空域地形纯函数与 Konva 最底层渲染：Voronoi 归属、距离衰减、未知区域降透明度、稀疏装饰和 2000 格保护。
- 已补 `terrain.test.ts`，待运行前端测试、类型检查、构建与浏览器视觉验收。

## TODO

- 实现并验证空域地形场。
- 实现并验证地貌选择器。
- 实现并验证 AI 一键补全。
