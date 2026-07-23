# 沙盘补全三件套：AI 一键补全 + 地貌选择器 + 空域地形场（实施文档）

> 本文档面向**独立执行的 AI 编码助手**，自带全部上下文，按序实施即可。
> 2026-07-23 与用户对齐的需求；沙盘本体（P-Hex-1~4）已合入 master
> （提交 2b7c6b8 / 9b9ea91 / cbbceba / b4e2cd8），本文是其后续三个任务。

---

## 〇、背景与现有架构（必读）

**技术栈**：前端 React + TypeScript + Vite + Tailwind（`apps/web/`）；后端 FastAPI +
SQLAlchemy + SQLite（`server/`）。

**沙盘数据契约**：每个模组场景（`Module.scenes` JSON 数组的元素）可带

```json
"map": {"q": 0, "r": -2, "biome": "urban"}
```

- `q/r`：pointy-top axial 六边形坐标（东 +q；正北为 (+1,-2) 方向）。坐标是**象征性
  相对位置**——只承诺方位与相对远近，不承诺比例尺。
- `biome` 十选一：`plain / forest / water / coast / desert / mountain / swamp / urban /
  ruin / interior`（单一真源：`server/app/services/hex_map.py` 的 `BIOMES` 与中文名
  `BIOME_LABELS`）。
- `kind == "chapter"` 的场景是叙事章节，**不上沙盘**、不允许有 map。

**关键文件**：
- `server/app/services/hex_map.py`：坐标数学（`axial_distance`/`direction_word`/
  `distance_word`）、落位修复器 `ensure_scene_maps(scenes)`（原则「只补洞不推翻」：
  合法坐标保留，缺失/撞格按已定位邻居重心+螺旋找空位确定性落位；biome 非法归
  `plain`；chapter 的误给 map 会被清掉）、懒回填 `ensure_module_map(db, module)`
  （`GET /modules/{id}` 与 `GET /sessions/{id}/locations` 都会调用，幂等）、
  KP 手动落位 `set_scene_map`。
- `server/app/services/module_service.py`：`PARSE_PROMPT_TEMPLATE`（解析规则 16 已让
  LLM 在**新导入**时提议 map）、`_normalize_scenes`（关键词补全 + 落位修复，
  create/update 共用）。
- `apps/web/src/components/game/HexSandbox.tsx`：konva 渲染（场景瓦片 + 目前只有
  **一圈**邻格地貌晕染 `halos` + 麻绳连线 + 三态迷雾 + 名牌层）；`BIOME` 样式表
  （每地貌 fill/deco 色）、`hexXY`/`xyToHex`、确定性伪随机 `rng(q,r)`、装饰群落
  `HexDeco`。props：`locations / disabled / onPick? / editable? / onMoveScene?`。
- `apps/web/src/pages/ModuleDetailPage.tsx`：模组详情页，页签 `detail/graph/timeline/
  sandbox`；沙盘页签查看=作者上帝视角、编辑=拖拽落位（本地改 `data.scenes[i].map`，
  随「保存」PUT 落库）；`moveScene` 有撞格校验。
- `apps/web/src/pages/GameSessionPage.tsx`：游戏内大地图弹窗双页签（沙盘/调查板），
  真人 KP 有上帝/玩家视角切换（按 `known` 字段客户端过滤）。

**现状问题（本文要解决的）**：
1. 旧模组（沙盘功能上线前导入的）靠懒回填拿到坐标，但回填是确定性程序，不做语义
   推断——**biome 全部是默认 plain**，且部分模组解析时 `connections` 缺失严重
   （如「呼兰大侠」11 个地点 9 个无连接，沙盘无路网、KP 方位段退化、落位散乱）。
2. 编辑器只能拖坐标，**没有改地貌的 UI**。
3. 沙盘上只有场景格 + 一圈淡晕染，场景之间/周围的**空域是黑的**，观感是「悬浮孤岛」
   而非连续大陆。

**LLM 调用约定（硬性）**：
- 一律**不设 max_tokens**（项目硬约定）。
- 走项目的 Provider 抽象：`from app.ai.llm_factory import get_fast_llm`（结构化副任务
  用快模型，未配置自动回落主模型）；调用形如
  `await llm.complete(messages, temperature=0, response_format={"type": "json_object"})`。
- fail-open：LLM 失败/坏 JSON 时不落库、返回明确错误，绝不写脏数据。

**工程约定（硬性）**：
- 图标一律 `react-icons/gi` 或 `lucide-react` 矢量，**禁止 emoji/符号字符**（前后端）。
- 所有回复与 git commit 用中文，commit 末尾加
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 改后端跑 `cd server && .venv/bin/pytest -q && .venv/bin/ruff check .`；
  改前端跑 `cd apps/web && npx tsc --noEmit && npx vite build`。
- 新行为补单测，不破坏既有断言（沙盘相关既有测试：`server/tests/test_hex_map.py`）。

**红线（不得触碰）**：
- 不做场景**内部**几何（房间/墙/瓦片图，2026-07-04 已删过一次，勿重建）。
- 不引入比例尺/格间移动力/hex 寻路——travel 仍走 `connections` 图校验。
- 空域地形是**纯视觉层**，不得引入任何游戏语义、不落库（见任务三）。

---

## 一、任务一：AI 一键补全（地貌 + 连接 + 语义重排）

### 目标
对**已入库**的模组做一次轻量 LLM 调用，补全三样东西：
1. 每个 location 场景的 `biome`（按场景标题/描述/氛围推断）；
2. 缺失的 `connections`（只增不删，语义=「物理直连、一步可达」）；
3. 语义化的坐标重排提议（「镇北的教堂放北侧」——旧模组的回填坐标只有拓扑没有地理）。

### 后端

新文件 `server/app/services/module_map_service.py`：

```python
async def enrich_module_map(db, module) -> dict:
    """一次 LLM 调用补全地貌/连接/落位，确定性校验后落库。返回 {updated: bool, ...摘要}"""
```

实现要点：
- **输入材料**：模组 title / description / world_setting（era/region/location/tone）+
  每个 location 场景的 `{id, title, description(截断~200字), danger, atmosphere,
  connections(现有)}`。**不要**把 truth/secrets/clues 喂进去（无必要，少一分泄露面）。
- **Prompt 要求输出** JSON：
  ```json
  {"scenes": [{"id": "scene_1", "biome": "urban", "q": 0, "r": -2,
               "add_connections": ["scene_2"]}]}
  ```
  措辞要点（与解析规则 16 同一坐标语义）：q/r 为 axial 整数，东 +q、正北 (+1,-2) 向；
  相连场景距离 1-3 格、坐标不重叠；线性结构沿直线排；室内房间/车厢用 interior；
  connections 只标「物理上直接相连、一步可达」（有门/通道/楼梯直通），开放城市里
  「都在街上、随便走」的地点**不要**强行连边；biome 十选一。
- **确定性校验与合并**（全部在代码里，不信任 LLM 输出）：
  - 只处理 `kind != "chapter"` 且 id 能对上的场景；未知 id 忽略。
  - `biome`：不在 `hex_map.BIOMES` 内 → 丢弃该项（保留原值）。
  - `add_connections`：目标 id 必须存在且非自身；**并集合并，绝不删除既有连接**；
    同时把无向对称性交给现有 `_scene_adjacency`（存单向即可，读取时闭包）。
  - 坐标：把 LLM 提议写进 scenes 副本后整体过 `hex_map.ensure_scene_maps`（它会保留
    合法提议、修复撞格/缺失）。
  - 全量替换 `module.scenes`（JSON 列必须整体重赋值才会落库，参照
    `ensure_module_map` 的写法）。
- LLM 异常 / JSON 解析失败 → 抛 ValueError（API 层转 400），**不落库**。

新端点（`server/app/api/modules.py`）：

```python
@router.post("/{module_id}/map/enrich")
async def enrich_map(module_id: str, db=Depends(get_db)):
    """AI 补全沙盘：地貌+连接+语义落位。同步调用（单次轻量 LLM，秒级）。"""
```

404（模组不存在）/ 400（LLM 失败，带原因）。模组端点目前均无鉴权（本地部署），
与现状保持一致即可。

### 前端

`ModuleDetailPage.tsx` 沙盘页签（查看与编辑模式均可用）加按钮：

- 文案「AI 补全地貌与连接」，图标用 `lucide-react` 的 `Sparkles`（页面已 import 过
  同款用于生成角色，注意查重导入）。
- 点击弹确认（复用 `components/ui/confirm-dialog` 的 `ConfirmDialog`，页面里有现成
  用法）：告知「将由 AI 重排场景落位、补全地貌与连接；已有连接不会被删除；
  之后仍可拖拽微调」。
- 请求期间按钮 loading 态；成功后**重新拉取模组数据**刷新沙盘（编辑模式下若本地
  有未保存改动，提示会被覆盖——简单做法：编辑模式下置灰该按钮，只在查看模式提供，
  避免与本地表单状态打架，推荐此简化）。
- 失败 toast 错误信息。

### 测试（不调真 LLM）

`server/tests/test_module_map_enrich.py`：mock 一个 fake llm（参照
`tests/test_evals.py` 里 `TestImprovisedPromotion.test_generate_npc_card_强制name且secrets空`
的 `_LLM` 假对象模式，`complete` 直接返回构造好的 JSON 字符串），断言：
1. biome 合法值被采纳、非法值保留原值；
2. add_connections 并集合并、不删既有、未知 id/自连被丢弃；
3. 坐标提议经修复器后无撞格、chapter 不被处理；
4. LLM 返回坏 JSON → 抛错且 `module.scenes` 未变；
5. 幂等性不要求（每次 enrich 可产生不同布局是预期行为）。

---

## 二、任务二：编辑器地貌选择器

### 目标
不重新导入也能手工指定某场景的地貌。

### 实现

1. **前端共享枚举**：新建 `apps/web/src/lib/biome.ts`：
   ```ts
   export const BIOMES = ['plain','forest','water','coast','desert','mountain',
     'swamp','urban','ruin','interior'] as const
   export const BIOME_LABELS: Record<string,string> = { plain:'原野', forest:'密林',
     water:'水域', coast:'海岸', desert:'荒漠', mountain:'山地', swamp:'沼泽',
     urban:'城镇', ruin:'废墟', interior:'室内' }
   ```
   ——**必须与后端 `hex_map.BIOMES/BIOME_LABELS` 十值一致**；`HexSandbox.tsx` 里的
   `BIOME` 样式表键也要能覆盖这十个值（已覆盖，改动时留意）。

2. **场景卡加选择器**：`ModuleDetailPage.tsx` 详情页签的场景 Section（编辑模式）里，
   在「危险度」Select 旁加一个「地貌」Select（项目用 `@/components/ui/select` 的
   Select/SelectTrigger/SelectItem，参照同文件危险度选择器的现成写法）。
   - 值：`data.scenes[i].map?.biome ?? 'plain'`；
   - 改动：`updScene(i, { map: { ...(s.map || {}), biome: v } })`——**注意**：场景可能
     还没有坐标（`map` 为空或只有 biome），这是合法中间态：后端 `ensure_scene_maps`
     在保存时会保留 biome 并补 q/r（`biome` 从既有 map dict 读取，已验证）。
   - 查看模式显示中文地貌名（badge 即可）。

3. （可选加分项）沙盘页签编辑模式下，选中场景后在画布角落显示一个小地貌下拉，
   改动同样写进本地 `data`。不做也不影响验收。

### 测试
前端无既有组件测试框架负担（项目仅 `*.test.ts` 纯函数测试，vitest 风格，如
`components/game/diceNotation.test.ts`）；本任务以 `tsc + build` 通过 + 手动
浏览器验证为准。后端无需改动（`_normalize_scenes` 已兼容）。

---

## 三、任务三：空域地形场（连续大陆感）

### 目标
沙盘上除场景格以外的空域自动填充地形，使版面呈现连续地图而非悬浮孤岛。

### 设计决策（已定，勿改）

- **纯前端渲染层、不落库、无游戏语义**。空域格不可点击、不参与 travel/迷雾逻辑；
  KP 拖拽场景后地形场自动跟随重算。禁止为空域新增任何后端字段或接口。
- **确定性**：同一份场景数据渲染结果永远相同。禁用 `Math.random()`，一律用现有的
  `rng(q,r)` 哈希伪随机（`HexSandbox.tsx` 里已有）。
- **防剧透（最重要）**：地形场**只能从传入组件的 `locations` prop 推导**。玩家视角
  的 payload 本来就只含已知场景，因此玩家看到的地形场只由已知场景生成——绝不能
  让玩家从「远处一片水域」反推出未知的港口场景。KP 上帝视角/模组页作者视角拿到
  全量场景，地形场自然更完整。**不要**从别的渠道（如另发请求拿全量）获取数据。

### 算法（在 `HexSandbox.tsx` 内实现，替换现有一圈 `halos` 逻辑）

1. **范围**：已落位场景坐标包围盒向外扩 3 圈（`FIELD_MARGIN = 3`）；总格数上限
   `FIELD_CAP = 2000`，超出时把 margin 逐级降到 2/1（超大模组保护，konva 单层
   数百个多边形无压力，数千会卡）。
2. **归属**：每个空域格取**最近场景**（`axial_distance`）的 biome（Voronoi）。
   为让边界有机而非直线：比较距离时加确定性扰动
   `d' = d + (rng(q,r)() - 0.5) * 0.9`；并列时按场景在数组中的序取先者。
3. **强度衰减**：空域格填充透明度随到最近场景的距离衰减，如
   `opacity = max(0.06, 0.22 - 0.045 * d)`——近处像场景的«势力范围»，远处淡出到
   画布底色（烛光暗角自然收边）。
4. **稀疏装饰**：`rng` 判定约 1/4 的空域格叠一份 `HexDeco`（复用现有装饰群落，
   传低 opacity），密度必须明显低于场景格，避免喧宾夺主。
5. **层序**：地形场画在最底层（现 `halos` 的位置），之上依次是连线、场景瓦片、
   名牌层。全部 `listening={false}`。
6. **迷雾协调**：`known === false` 的场景（仅 KP 上帝视角存在）参与地形场推导时，
   其周边空域格照常上色但整体再压一档透明度（乘 0.6），与「未探明」瓦片的浓雾
   观感一致。玩家视角不存在该分支（数据已被过滤）。
7. 抽一个**纯函数** `terrainField(located: {q,r,biome,known?}[]): {q,r,biome,opacity}[]`
   便于单测（放同文件 export，或 `apps/web/src/lib/terrain.ts`）。

### 测试
`apps/web/src/lib/terrain.test.ts`（vitest 风格，参照 `diceNotation.test.ts`）：
1. 确定性：同输入两次调用结果深等；
2. 场景格本身不出现在空域结果里；
3. 空域格 biome 等于（扰动意义下）最近场景的 biome——用相距很远的两个场景断言
   各自邻域归属；
4. FIELD_CAP 生效（构造大包围盒输入，结果数 ≤ 上限）。

---

## 四、实施顺序与验收清单

| 序 | 任务 | 验收 |
|---|---|---|
| 1 | 任务三（空域地形场） | tsc+build 过；terrain.test 过；浏览器：模组页沙盘呈连续地形、拖拽场景后地形跟随、游戏内玩家视角地形只覆盖已知区域 |
| 2 | 任务二（地貌选择器） | tsc+build 过；浏览器：编辑改地貌→保存→沙盘瓦片样式变化；未落位场景改地貌保存后自动落位且 biome 保留 |
| 3 | 任务一（AI 补全） | pytest+ruff 过（含新测试）；浏览器：对「呼兰大侠」执行补全→出现路网连线与多样地貌、布局有地理语义；对坏 LLM 输出不落库 |

先做任务三的原因：它让任务一/二的效果立即可见（地貌一变，整片区域变色），
验收直观。

**每个任务独立 commit**（中文信息 + Co-Authored-By 尾行），不要合并成一个大提交。

## 五、已知边角与坑

- 「鬼屋」模组的 `scenes[].map` 残留**旧瓦片图**数据（`w/h/tiles/objects` 结构，来自
  已删除的场景内部地图功能）：`ensure_scene_maps` 会在首次触达时把它覆盖为新契约
  `{q,r,biome}`，这是**预期行为**（旧数据无读取方）。enrich 前无需特判。
- 「追书人」尚未回填（map 全空）：任务一执行时先调 `ensure_module_map` 或直接依赖
  enrich 自身的修复器路径即可，不需要用户先打开一次详情页。
- `Module.scenes` 是 JSON 列：**必须整体重赋值**（`module.scenes = new_list`）才会
  触发 SQLAlchemy 落库，原地 mutate 不生效——`hex_map.ensure_module_map` 是标准范例。
- 前端 `KnownLocation`/`SandboxLocation` 的 `map` 字段类型为
  `{q,r,biome} | null | undefined`，处理时留意空值。
- 解析 prompt（`PARSE_PROMPT_TEMPLATE`）中 JSON 大括号是 `{{ }}` 转义（str.format），
  若需改 prompt 切勿破坏转义。
