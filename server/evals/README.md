# KP 生成质量评估回路

与 pytest 分离的评测套件：重放真实会话的某一轮，重新生成 KP 叙事，
用「确定性检查 + 裁判模型」打分，产出 scorecard。**改 prompt 的 PR 应附
改动前后的 scorecard 对比**。

跑真模型、花钱、手动触发；用当前激活的 AI 配置（设置页 / .env）。

## 快速上手（在 `server/` 目录下）

```bash
# 1. 从真实会话导出 fixture
.venv/bin/python -m evals.snapshot --list                 # 列出会话
.venv/bin/python -m evals.snapshot <sid> --show           # 看事件尾部选切点
.venv/bin/python -m evals.snapshot <sid> --turn 42 \
    --name manor_interrogate --tags kp_core,npc --note "审讯管家，泄密危险轮"

# 2. 运行评估
.venv/bin/python -m evals.run --smoke        # 免费：只验证 fixture 可重建
.venv/bin/python -m evals.run --no-judge     # 便宜：生成 + 确定性检查
.venv/bin/python -m evals.run --suite kp_core  # 完整：+ 裁判模型逐项打分
.venv/bin/python -m evals.run --suite adjudication --no-judge --repeat 5
                                             # 每个 fixture 采样 5 次、报通过率（见「多次采样」）
.venv/bin/python -m evals.run --tool-loop    # 走 agent loop（工具调用）路径重放生成
                                             # （需 Provider 支持工具；工具调用会序列化回
                                             #   方括号指令形态参与打分，口径与旧路径一致）

# 3. 对比两次运行
.venv/bin/python -m evals.compare results/<旧>.json results/<新>.json
```

## 切点怎么选

`--turn` 应指向**玩家本轮输入的最后一条事件**（action/dialogue）：重放时以
截至该事件的历史重新生成 KP 回合，等价于「让当前代码版本重新回应当年那次输入」。
用 `--show` 预览事件尾部确认。

## 预存计划 vs 现跑计划

- fixture 里 `plan` 非空（`--with-plan` 导出）：重放只评 **KP 叙事**，
  跨版本对比更稳，适合改 KP prompt 时用。
- `plan` 为空：重放时现跑 turn_planner，评的是 **planner + KP 端到端**，
  适合改 planner prompt 时用。

## 打分口径

- **确定性检查**（evals/checks.py，免费）：内部标识泄漏、汇报体、指令语法、
  替玩家开口启发式、以及 **planner 裁定断言**（`plan_adjudication`，见下）。
  `error` 计入不通过，`warn` 仅提示。
- **裁判模型**（evals/judge.py）：no_leak / plan_adherence / no_player_control /
  in_character / coherence 逐项 0/1。裁判调用失败记 `judge_error`，该 fixture
  按不通过处理（宁可假阴性，不给假分）。
- LLM 生成有随机性：单项翻转看两次运行是否复现，再下结论——或直接用 `--repeat` 取通过率。

## 裁定断言（plan_expect，评 planner 的裁定质量）

评「planner 是否据虚构态势正确调节难度 / 奖惩骰 / 免检」这类**裁定质量**时，在 fixture
顶层加 `plan_expect`：对现跑出的 `plan` 做 `any_of` 断言（任一子句满足即通过），免费、不调 LLM。

```jsonc
"plan_expect": {
  "note": "噪音已把循声怪引来，潜行应变难或直接失败",
  "any_of": [
    { "path": "check.penalty",    "op": ">=", "value": 1 },
    { "path": "check.difficulty", "op": "in", "value": ["hard", "extreme"] },
    { "path": "auto_outcome",     "op": "==", "value": "failure" }
  ]
}
```

`path` 点分进 `plan`（`check.penalty`…）；`op` ∈ `== != >= <= > < in`。这类 fixture 要
**不预存 plan**（现跑 planner）并打 `adjudication` tag。参考 `fixtures/synthetic_*_after_noise`、
`fixtures/synthetic_persuade_strong_rp`。

## 多次采样（--repeat N，抵消 LLM 波动）

`--repeat N` 让每个 fixture 采样 N 次，报**通过率**与**稳过判定**（全 N 次都过才算「稳过」），
明细汇总各次失败原因的命中次数（如 `plan_adjudication×3`）。

单次采样会骗人：曾见「噪音后潜行」单跑 `penalty=1` 像稳，`--repeat 5` 一测只 2/5——多采样
既稳定了 scorecard，也把偶发的**计划回退 bug**（模型把某字段写成 int/null 撞 schema，令整份
TurnPlan 校验失败退回旧流程）压了出来。改 planner/裁定相关 prompt 时优先用 `--repeat` 出结论。

`compare.py` 已按通过率逐项 diff，直接对比两次 `--repeat` 的 scorecard 即可。

## 评测集建设目标（tags 约定）

初始目标 10~15 个 fixture，覆盖以下轮型（tag 标注）：

| tag | 轮型 |
|---|---|
| `opening` | 开场（无历史事件） |
| `check` | 检定裁定轮 |
| `adjudication` | 裁定质量轮（据虚构态势调难度/奖惩骰/免检，配 `plan_expect`） |
| `npc` | NPC 对话轮 |
| `split` | 分头行动 |
| `blind` | 暗投（心理学等）后的叙事轮 |
| `stuck` | 玩家卡关、绕圈的轮次 |
| `leak_risk` | 临近泄密的危险轮（do_not_reveal 非空且诱惑大） |
| `kp_core` | 核心回归集（每次改 prompt 必跑） |
| `synthetic` | 合成用例（非真实会话导出） |

`fixtures/` 入库共享；`results/` 是本地运行产物，不入库。
