# 多人 roadmap 阶段 2：匹配大厅（创建房间 → 准备 → 满员开局）设计稿

**目标**：把"开始游戏"重构为"创建房间 + 匹配大厅"。房主建房后进入大厅；非房主用房间码进房，可看房间情况（模组简介、各席位/队友角色卡）、选自己的角色、切准备态；**所有席位填满且所有真人已准备**后，房主才能开局。大厅内可聊天。

**与 2026-06-27 决策对齐**：跨机联机用**主机制**——以房主的后端为唯一权威 server，其他人连它。

---

## 0. 连接模型（先解决"加不进房间"）

**根因**：Mac 与 Windows 各跑一套后端 + 各自独立 SQLite 库；房间只存在于房主（Mac）的库里，而前端 `BASE='/api'` 永远打本机后端。

**方案（主机制 + 浏览器直连）**：
- 房主：后端照常 `127.0.0.1:8000`；前端 vite 用 `--host` 暴露到局域网（`dev:web` 加 `--host`）。
- 其他人：浏览器直接打开 `http://<房主局域网IP>:5173`，**本机不跑任何服务**。
- 全场只有一套后端/一套库 → 房间码可达；token 存各自浏览器 localStorage → 身份天然区分；浏览器→房主 vite 同源、vite→后端本地代理 → **无跨域**。
- 可选增强：vite proxy target 支持 `VITE_API_TARGET` 环境变量，便于"各自跑前端、指向房主后端"的备选用法（需房主后端 `--host 0.0.0.0` + CORS 放开，非默认路径）。

> 公网可达 + 分享链接留到阶段 3。

## 1. 状态机

会话 `status` 复用现有枚举：
- `setup`：**大厅**。房间已建、席位待填/待准备，未开局。
- `active`：进行中（已开局，触发过 opening）。
- `paused` / `ended`：照旧。

转换：建房 → `setup`；房主点"开始" 且通过门槛校验 → `active`（并触发 opening 生成）。不可逆回退到 `setup`。

## 2. 数据模型

`session_participants` 新增：
- `ready: bool = False`：该席位是否已准备。AI 席视为恒 `True`（自动就绪）。空 human 席（未认领）`ready=False`。

迁移：alembic 加 `ready` 列（默认 False，回填存量为 True 以兼容已开局会话）。

无需新表；房主身份用现有 `is_primary` 席位的 `owner_token` 表达（建房者 token = 房主）。

## 3. 后端端点

复用现有：`POST /sessions`（建房）、`GET /sessions/by-code/{code}`、`POST /{id}/claim`、`GET /{id}`、`GET /{id}/live`、`POST /{id}/opening`。

**改动**：
- `POST /sessions` 增加 `as_lobby: bool`（或按"是否存在空 human 席"判定）：大厅模式建为 `status="setup"`，**不立即触发 opening**，房主进大厅页而非游戏页。
- 现有"全 AI/主角直开"路径保持：无空席且 `as_lobby=false` 时仍可直接 `active` 开局（单人体验不回退）。

**新增**：
- `POST /{id}/ready`：body `{ready: bool}`；把"当前 token 拥有的席位"的 `ready` 置位；广播 `lobby` 事件刷新大厅。
- `POST /{id}/start`：仅房主（token == 主角席 owner_token）可调；校验**所有席位已填角色 + 所有真人席 ready**；通过则 `setup→active` 并 `generation_manager.start(opening)`；广播 `started`。不通过返回 400 + 缺口说明。
- `POST /{id}/lobby-chat`：大厅聊天（开局前）。落 `ooc` 事件 + 广播。复用 OOC 渲染，省一套消息类型。
- `GET /{id}` / `by-code` 的 participant 输出补 `ready`、`is_host`（=该 token 是否房主）。

**校验/边界**：
- claim 已有 token 绑定；补：claim 只能认领空 human 席；同一 token 在一个房间只占一席。
- start 门槛：`every(seat: seat.character_id != null) && every(human seat: ready)`；至少 1 真人。
- 防重复 opening 已由生成层幂等保证。

## 4. 前端

**路由**：新增大厅页 `RoomLobbyPage`（`/room/:sessionId`）。建房/进房后若 `status==='setup'` → 进大厅；`active` → 进游戏页（现有 `GameSessionPage`）。

**GamePage（开始游戏页）重构**：
- "新游戏"→"创建房间"：选模组 + 配席位（沿用现有席位编辑器：主角/AI/留空真人席）→ 建房进大厅。
- "加入房间"：输入房间码 → 进大厅页（不再在卡片里就地选角色；选角色挪到大厅内）。

**RoomLobbyPage（核心新页）**：
- 顶部：模组标题 + **简介**（`description`）、房间码（可复制）、房主标识。
- 席位列表：每席显示 角色卡缩略（点开看详情，复用 `CharacterPanel`）/空席/谁是真人谁是 AI/准备态徽标。
- 我的席位操作：选已有角色或 AI 现造 → claim；已入座后显示"准备/取消准备"开关。
- 大厅聊天区：消息流（走 `/live` 的 `lobby`/`ooc` 事件）+ 输入框。
- 底部：房主看到"开始游戏"按钮，未满足门槛时禁用并提示缺口（"还有 2 个空席 / 张三未准备"）；非房主看到"等待房主开始"。
- 实时：复用 `/live` 自动重连，收到 `lobby` 刷新席位、`seat`/`ready` 更新、`started` → 跳转游戏页。

**store**：`createRoom`（status=setup）、`joinByCode`、`claimSeat`、`toggleReady`、`startGame`、`lobbyChat`；GameSession 类型补 `status`/`participants.ready`/`is_host`。

## 5. 复用与不重做

- `RoomHub` / `/live` / `stream_room` / in-flight buffer / 自动重连：直接复用，大厅与游戏共用同一实时通道。
- `claim_seat` / `owner_token` / `room_code`：已存在，补 ready 与 host 校验即可。
- OOC 广播：大厅聊天复用，不新增消息类型。
- 席位编辑器、AI 建卡、PartyRoster、CharacterPanel：复用。

## 6. 测试

后端：
1. 建房为 `setup`、不触发 opening。
2. ready 置位只改自己席位；AI 席恒 ready。
3. start 门槛：有空席 / 有人未准备 → 拒绝；满足 → `active` + 触发 opening。
4. 非房主调 start → 拒绝。
5. claim 只能认领空 human 席、一 token 一席。

前端：tsc + build；大厅页渲染/门槛禁用逻辑回归。

## 7. 非目标

- 公网部署 / 打洞 / 分享链接（阶段 3）。
- 多 KP、真人当 KP。
- 大厅内复杂权限（踢人、转让房主）——先只做最小：房主开局。
- 断线席位回收 / 超时——先不做。

## 8. 落地顺序

1. 后端数据模型 + 迁移（participant.ready）。
2. 后端服务/端点（建房 setup、ready、start 门槛、lobby-chat）+ 测试。
3. 前端 store + RoomLobbyPage + GamePage 重构。
4. 连接模型：dev:web 加 `--host`，文档化"浏览器直连主机"。
5. 端到端：同机双浏览器窗口验证大厅→准备→开局；再跨机验证。
