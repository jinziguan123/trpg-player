# 多人 roadmap 阶段 2 实施计划：真人联机（实时房间 + 身份席位 + 多人回合）

> 设计稿见同目录 `...-phase2-realtime-rooms-design.md`。决策：2a+2b+2c 一次做完；回合自由式；token 明文 MVP；仅局域网。

**总体策略：** 先地基（RoomHub 常驻广播）→ 身份与席位 → 多人回合，每段带测试。后端先行、前端随后。坚持复用 SSE 单向广播 + POST 上行，不引入 WebSocket。

---

## T1（2a）实时房间频道：RoomHub + 常驻 /live + 持久重放

**后端**
- 新增 `app/services/room_hub.py`：`RoomHub`，`room_id -> set[Queue]` **常驻**订阅集合；`subscribe/unsubscribe/broadcast`；保留每房间「当前生成 in-flight buffer」供中途接入重放流式 token。
- `generation_manager.publish` 改为把 chunk 投递进 `RoomHub.broadcast`（生成是生产者，RoomHub 是唯一广播出口）。`GenerationManager` 仍管生成 task 与防并发。
- 事件 schema 统一加 `seq`：离散持久事件（action/dialogue/dice/system/ooc/narration_final）带 `event_logs.sequence_num`；流式 narration token 不带 seq，仅实时下发。
- 新增 `GET /api/sessions/{id}/live?after_seq=N`：先 subscribe（开始缓冲实时），再从 `event_logs` 重放 seq>N 的离散事件，然后接入实时流；含 in-flight buffer 重放。
- 行动/OOC 落库后经 RoomHub 广播离散事件给全房间（带 seq）；KP narration 对在线者实时流 token、对重连者靠 narration_final 持久事件重放。

**前端**
- 进游戏页即建立常驻 `/live` SSE（取代「仅生成时订阅」）；统一消费房间事件，按 `seq` 去重（客户端记 maxSeq，重连用 after_seq=maxSeq）。
- 自己的输入仍乐观回显；权威以广播为准。

**验证：** 同一局两个浏览器标签，A 发消息→B 实时看到 A 行动 + KP 叙事，刷新 B 后从 event_logs 正确重放。

---

## T2（2b）身份与席位认领

**数据模型 + 迁移**
- `Character.owner_token: str | None`。
- `GameSession.room_code: str`（建房生成、唯一短码）。
- `SessionParticipant.owner_token: str | None`、`character_id` 改可空、新增 `claimed: bool`（human 空席先建、后认领填角色）。
- 迁移：加列；回填旧会话 room_code、主角席 owner_token（用房主 token 或占位）。

**后端**
- 轻量 token：请求头 `X-Player-Token`，依赖项解析（无则匿名/拒绝按端点）。
- 建房：扩展 `create_session` 支持 human 空席（claimed=false、无 character）；生成 room_code；房主席 owner_token=请求 token。
- `POST /api/rooms/join`（或 `/sessions/by-code/{code}`）：按 room_code 取房间 + 席位状态。
- `POST /api/sessions/{id}/claim`：认领一个空 human 席，带 character_id（已有自己角色或先创建），写 owner_token 到席位与角色。
- 入座/认领后经 RoomHub 广播 `seat` 事件。

**前端**
- 大厅：建房（沿用阶段 1 席位编辑器，human 席可设「留空待加入」）/ 加入（输 room_code）。
- 房间视图：席位与在场展示、认领空席（选/造角色入座）。
- token：首次生成 UUID 存 localStorage，注入请求头。

**验证：** A 建房（1 主角席 + 1 空 human 席 + 1 AI 席）分享 code；B 另一浏览器加入、认领、入座；A 实时见 B 入座。

---

## T3（2c）多人回合（自由式）

**后端**
- `chat` / `ooc` 接收 `acting_character_id`；校验该角色席位 `owner_token == 请求 token`。
- 自由式 + 单生成锁：`is_generating(room_id)` 时他人提交→409「KP 正在叙事」。
- 队友自动响应沿用阶段 1，结果经 RoomHub 广播全房间。
- `presence`：/live 连接建立/断开时广播在线成员（简单计数/名单）。

**前端**
- 以「自己认领的角色」身份发消息（actor=该角色）。
- KP 生成中对他人禁用输入并提示。

**验证：** 2 真人 + 1 AI 的局，A、B 自由行动，彼此 + 队友 + KP 实时同步不串。

---

## T4 端到端回归
- 后端 pytest 全绿（新增 room_hub / 身份席位 / 多人回合用例）。
- tsc + build 绿。
- 双浏览器实机：建房→加入→入座→多人自由行动→队友/ KP 实时同步。
- 兼容：旧单人会话仍可正常进入与游玩。
