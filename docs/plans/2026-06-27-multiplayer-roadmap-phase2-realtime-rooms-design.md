# 多人 roadmap 阶段 2：真人联机（实时房间 + 身份席位 + 多人回合）设计稿

**目标：** 让多名真人玩家通过同一「房间」一起跑团，空席由 AI 队友补位；所有人实时看到彼此的行动、KP 叙事、检定与 OOC。本阶段面向**同主机 / 局域网**（房主后端即房间 server），公网可达与分享链接留给阶段 3。

**关键既有资产：** `GenerationManager.publish` 已能向同一 session 的多个订阅者广播；`session_participants` 已是多席位事实来源（human/ai + is_primary + seat_order）；事件流 `event_logs` 已持久化所有发生过的事。阶段 2 主要是把"生成期单播"升级为"房间级常驻广播"，并补上身份与多人回合。

---

## 一、现状与缺口（为什么不能直接多人）

| 维度 | 现状 | 缺口 |
|---|---|---|
| 实时通道 | `GenerationManager` 的订阅者集合是**按 session_id、但生命周期绑定单次生成**：`_on_done` 给所有订阅者发 `None` 关流并清 buffer | 两次生成之间无常驻通道；玩家 A 提交的行动、OOC、入座事件无法实时推给玩家 B |
| 回合/行动者 | `chat` 端点固定用 `session.player_character_id` 作为行动者 | 多真人时需「谁在以哪个角色行动」，且要校验该玩家确实拥有那个席位 |
| 身份 | 无 token、Character 无归属 | 需要轻量 player token，Character.owner_token，席位认领 |
| 加入方式 | 开局即一次性创建所有席位+角色 | 需要房间码、空席（human 未认领）、他人加入后认领并带/造角色 |
| 重连补全 | 仅靠生成期内存 buffer 重放 | 常驻通道下，重连要从 `event_logs` 重放（持久、跨多次生成） |

**结论：** 核心改动是引入**房间级常驻事件通道（RoomHub）**，`GenerationManager` 降级为"向 RoomHub 投递生成 chunk 的生产者之一"。身份/席位/回合都建立在这个通道之上。

---

## 二、子分期（每段独立可验证）

- **2a 实时房间频道（地基）**：每个 session=房间有一条**常驻 SSE 通道**，所有成员订阅、跨生成存活。玩家行动、KP 叙事、队友响应、检定、OOC、入座/在场事件统一经此通道广播给所有成员。重连从 `event_logs` 按 seq 重放。
  - *可验证*：同一局在两个浏览器标签打开，A 发消息，B 实时看到 A 的行动 + KP 叙事，无需刷新。
- **2b 身份与席位认领**：轻量 player token（前端 localStorage 生成、随请求头带上）；`Character.owner_token`；房间码；human 空席；加入房间→认领空席→带已有角色或现造角色入座。
  - *可验证*：A 建房（自己 1 席 + 1 空 human 席 + 1 AI 席），分享房间码；B 用另一浏览器加入、认领空席、选角色入座，A 侧实时看到 B 入座。
- **2c 多人回合**：`chat` 接收"行动角色 id"，按 token 校验其归属席位；同一房间同一时刻只允许一次 KP 生成（沿用 `is_generating` 锁）；AI 队友在真人行动后仍自动响应。
  - *可验证*：2 真人 + 1 AI 的局，A、B 轮流/自由行动，彼此与队友、KP 都实时同步且不串。

**建议先做 2a**——它是地基，且能独立演示（双标签实时同步）。2b/2c 在其上叠加。

---

## 三、数据模型改动

### Character
- 新增 `owner_token: str | None`（归属某玩家 token；AI 角色为 None）。

### GameSession（即"房间"）
- 新增 `room_code: str`（短码，分享/加入用；建房时生成、唯一）。
- 复用现有 `status`（active/paused）；房间不引入独立表，session 即房间。

### SessionParticipant（席位）
- 新增 `owner_token: str | None`：human 席位被某玩家认领后写入；AI 席位为 None。
- 新增 `claimed: bool`（或用 `character_id is None` 表示空席）：human 席位可先空着等人认领。
  - *权衡*：当前 participant 建时即绑 character_id。2b 要允许"先建空 human 席、后认领填角色"，故 character_id 需可空 + claimed 标记。
- `is_primary` 保留为房主席（建房人）。

### 兼容
- 单人/旧会话：建 session 时自动给主角席生成 `owner_token`=房主 token、`room_code`；行为与现在一致。

---

## 四、实时通道设计（2a 核心）

### RoomHub（新增，独立于 GenerationManager）
- 维护 `room_id -> set[Queue]` 的**常驻**订阅者集合，**不随生成结束而清空**。
- `broadcast(room_id, event)`：向该房间所有订阅者投递一条事件（含 seq、type、actor、payload）。
- 成员通过 `GET /api/sessions/{id}/live` 建立**长期 SSE**（区别于现在按生成开关的 `/stream`）。
- 断线/刷新重连：客户端带上"已收到的最大 seq"，服务端先从 `event_logs` 重放 seq 之后的事件，再接入实时流——**重放源从内存 buffer 换成持久 event_logs**，跨多次生成可靠。

### 生成与通道的关系
- `GenerationManager` 仍负责"单次 KP/队友生成 task 的执行 + 防并发"，但其 `publish` 改为**投递进 RoomHub.broadcast**（而非自己维护订阅者）。
- 即：generation 是"事件生产者之一"，RoomHub 是"唯一的房间广播出口"。流式 token（narration 增量）也作为事件经 RoomHub 下发。

### 事件类型（统一 schema）
`action`（玩家行动）/ `dialogue` / `narration`(流式) / `dice` / `system` / `ooc` / `presence`(谁在线) / `seat`(入座/离座) / `generating`(KP 思考中) / `done`。前端按 type 渲染（已有大半）。

---

## 五、身份与席位（2b）

- **token**：前端首次进入生成 UUID 存 localStorage，之后所有请求带 `X-Player-Token`。无账号、无密码——token 即持有者凭证（MVP，安全性见第八节）。
- **建房**：房主选模组 → 设席位（沿用阶段 1 席位编辑器，但 human 席位可标记为"留空待加入"）→ 自己认领主角席并入座 → 生成 `room_code`。
- **加入**：输入 `room_code` → 看到房间席位状态 → 认领一个空 human 席 → 选已有自己角色或现造 → `owner_token` 写入该席与角色。
- **AI 席**：建房时即由房主用 AI 建卡填好（is_player=false），开局自动行动。

## 六、多人回合（2c）

- `chat` / `ooc` 端点改为接收 `acting_character_id`，并校验：该角色所属席位的 `owner_token == 请求 token`。
- **并发**：同一房间同一时刻仅一次 KP 生成（`is_generating(room_id)` 锁）；他人此时提交行动→排队或返回 409 提示"KP 正在叙事"。本阶段取**简单自由式**（谁先提交谁先被 KP 处理），不做 initiative。
- **AI 队友**：真人行动后仍跑一轮队友自动响应（阶段 1 逻辑），结果同样经 RoomHub 广播给所有人。

---

## 七、前端改动

- 进游戏页即建立常驻 `/live` SSE（取代"仅生成时订阅"），统一消费房间事件；本地乐观更新仅用于自己的输入回显，权威以广播事件为准（按 seq 去重）。
- 大厅/房间：建房（设席位）、加入（输房间码、认领空席、入座）、在场成员与席位状态展示。
- 行动者：玩家以"自己认领的角色"身份发消息；多角色时可切换（一般 1 真人 1 角色）。

## 八、安全与边界（重要）

- 本阶段**仅同主机 / 局域网**：房主后端即 server，其他玩家浏览器指向房主 IP:port。**不引入公网暴露、不引入 NAT 穿透**（阶段 3）。
- token 为明文 bearer，无加密无鉴权——局域网可信环境的 MVP 取舍。公网前必须补：传输加密、token 不可猜、房间码限流。**默认不监听 0.0.0.0、不开放公网**，由用户显式开启 LAN。
- CORS 现仅允许 localhost:5173/5174——LAN 多设备需放开到房主局域网来源（或同源部署打包后的前端）。

## 九、非目标（本阶段不做）
- 公网可达 / 分享链接 / 内网穿透（阶段 3）。
- 完整 initiative 战斗轮次。
- 账号体系 / 鉴权 / 多房主。
- 把 SSE 换成 WebSocket（坚持复用 SSE 单向广播 + 普通 POST 上行）。

## 十、待你拍板的决策
1. **本次迭代范围**：先只做 **2a 实时房间频道**（双标签实时同步地基），还是 2a+2b+2c 一次性做完整联机？
2. **回合模型**：自由式（谁先提交谁先被处理，单生成锁）／ 显式轮转 ／ KP 点名。建议自由式。
3. **身份**：localStorage UUID 明文 token + Character.owner_token，MVP 无鉴权——可接受否？
4. **部署边界**：确认本阶段只做同主机/局域网，公网留阶段 3。
