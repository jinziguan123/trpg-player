"""评估运行器：重放 fixture（build_kp_context → narrate），跑确定性检查 + 裁判打分。

用法（在 server/ 目录下）：
    python -m evals.run                      # 跑全部 fixture
    python -m evals.run --suite kp_core      # 只跑带某 tag 的
    python -m evals.run --fixture opening_x  # 只跑一个
    python -m evals.run --no-judge           # 只跑确定性检查（不花裁判调用）
    python -m evals.run --smoke              # 不调 LLM，只验证 fixture 可重建、上下文可构建
    python -m evals.run --tool-loop          # 走 agent loop（工具调用）路径重放生成

fixture 未预存 plan 时，重放会现跑一次 turn_planner（评的是 planner+KP 端到端）；
预存了 plan 则只评 KP 叙事本身（跨版本对比更稳）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.ai import turn_planner
from app.ai.agents.kp_agent import KPAgent
from app.ai.context import build_kp_context
from app.ai.llm_factory import get_llm

from evals import checks, judge
from evals.common import RESULTS_DIR, ReplayCase, iter_fixtures, load_fixture


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "nogit"


def _active_model_name() -> str:
    try:
        from app.api.ai_settings import load_active_profile
        profile = load_active_profile()
        return profile.model_name if profile else "env-fallback"
    except Exception:
        return "unknown"


def build_replay_messages(case: ReplayCase) -> list[dict]:
    """与 chat_service._run_generation 的单场景路径对齐：上下文 + 计划注入。"""
    messages = build_kp_context(
        case.session, case.module, case.player_char, case.events,
        teammates=case.teammates or None,
        rules_lookup_enabled=case.rules_lookup_enabled,
    )
    if case.plan is not None:
        messages.append(turn_planner.build_turn_plan_message(case.plan))
    return messages


async def _narrate_tool_loop(case: ReplayCase, llm, plan, messages: list[dict]) -> str:
    """--tool-loop：用 chat_service._run_kp_agent_loop（真实 loop 代码）重放生成。

    评估环境脱库，注入「干执行器」代替真实执行器：check 类工具视为已挂「待玩家掷骰」
    （suspend，与真人明骰的线上行为一致）；lookup/npc/state 类返回说明文本继续循环。
    模型发出的工具调用序列化回方括号指令形态、拼进旁白——判分口径
    （checks 的指令语法、judge 的「requires_check 时以 [DICE_CHECK] 收尾」）与旧路径一致。
    """
    from app.ai import tools as kp_tools
    from app.services import chat_service

    executed: list[str] = []

    async def dry_execute(call):
        spec = kp_tools.TOOLS_BY_NAME.get(call.name)
        if spec is None:
            return kp_tools.ToolOutcome(
                f"无此工具：{call.name}。只可调用系统提供的工具；若无需工具，直接继续叙述。"
            )
        # 文本形式指令（loop 的合成兜底，id 以 text_ 开头）已在原文里，不重复序列化
        if not str(call.id).startswith("text_"):
            executed.append(kp_tools.render_tag(spec, call.arguments))
        if spec.kind == "check":
            return kp_tools.ToolOutcome(
                "已向该玩家发出检定请求，等待其亲自掷骰。本轮叙述就此收束，绝不预测结果。",
                suspend=True,
            )
        if spec.kind == "lookup":
            return kp_tools.ToolOutcome(
                "（评估重放环境不可检索：请依据既有资料直接续写，不要再查阅。）"
            )
        if spec.kind == "npc":
            return kp_tools.ToolOutcome(
                "（评估重放：该 NPC 的台词由系统另行生成，请继续你的叙述，不要替其代言。）"
            )
        return kp_tools.ToolOutcome("ok")

    result = ["", "", [], [], []]
    async for _chunk in chat_service._run_kp_agent_loop(
        llm, messages, result, dry_execute,
        tools=kp_tools.openai_tool_schemas(), plan=plan,
    ):
        pass
    narration = result[1]
    if executed:
        narration = narration.rstrip() + "\n" + "\n".join(executed)
    return narration


async def run_case(case: ReplayCase, llm, use_judge: bool, tool_loop: bool = False) -> dict:
    # 「投骰后续写」重放：跳过 planner，改重放 KP_DICE_CONTINUATION_PROMPT——评的是续写阶段
    # 的行为（如叙述主语必须是检定执行者），故不走首段叙事、不注入 plan，也不进 tool loop。
    if case.continuation:
        from app.ai.prompts.kp_system import KP_DICE_CONTINUATION_PROMPT
        messages = build_kp_context(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
            rules_lookup_enabled=case.rules_lookup_enabled,
        )
        messages.append({
            "role": "user",
            "content": KP_DICE_CONTINUATION_PROMPT.format(dice_results=case.continuation),
        })
        kp = KPAgent(llm)
        narration = "".join([token async for token in kp.narrate(messages)])
        plan = None
        plan_source = "continuation"
        findings = checks.run_all_checks(narration, case.player_names)
        judge_result = await judge.run_judge(llm, case, plan, narration) if use_judge else None
        errors = [f for f in findings if f.severity == "error"]
        judge_failed = (
            [k for k, v in judge_result.items() if not v["pass"]] if judge_result else []
        )
        passed = not errors and not judge_failed and (judge_result is not None or not use_judge)
        return {
            "fixture": case.name,
            "tags": case.tags,
            "plan_source": plan_source,
            "plan": None,
            "narration": narration,
            "findings": [f.to_dict() for f in findings],
            "judge": judge_result,
            "judge_error": use_judge and judge_result is None,
            "passed": passed,
        }

    plan = case.plan
    plan_source = "fixture" if plan is not None else "live"
    if plan is None and case.events:
        plan_messages = turn_planner.build_turn_plan_messages(
            case.session, case.module, case.player_char, case.events,
            teammates=case.teammates or None,
            rules_lookup_enabled=case.rules_lookup_enabled,
        )
        plan = await turn_planner.run_turn_planner(llm, plan_messages)

    messages = build_kp_context(
        case.session, case.module, case.player_char, case.events,
        teammates=case.teammates or None,
        rules_lookup_enabled=case.rules_lookup_enabled,
    )
    if plan is not None:
        messages.append(turn_planner.build_turn_plan_message(plan))

    if tool_loop:
        from app.ai import tools as kp_tools
        messages.append(kp_tools.tool_mode_message())
        narration = await _narrate_tool_loop(case, llm, plan, messages)
    else:
        kp = KPAgent(llm)
        narration = "".join([token async for token in kp.narrate(messages)])

    findings = checks.run_all_checks(narration, case.player_names)
    # planner 裁定断言（虚构态势 → 难度/免检准则是否奏效）：免费、不调 LLM。
    findings += checks.check_plan_adjudication(
        plan.model_dump() if plan else None, case.plan_expect,
    )
    judge_result = None
    if use_judge:
        judge_result = await judge.run_judge(llm, case, plan, narration)

    errors = [f for f in findings if f.severity == "error"]
    judge_failed = (
        [k for k, v in judge_result.items() if not v["pass"]] if judge_result else []
    )
    passed = not errors and not judge_failed and (judge_result is not None or not use_judge)

    return {
        "fixture": case.name,
        "tags": case.tags,
        "plan_source": plan_source,
        "plan": plan.model_dump() if plan else None,
        "narration": narration,
        "findings": [f.to_dict() for f in findings],
        "judge": judge_result,
        "judge_error": use_judge and judge_result is None,
        "passed": passed,
    }


def run_smoke(paths: list[Path]) -> int:
    """不调 LLM：验证 fixture 可加载、ORM 可重建、KP/planner 上下文可构建。"""
    failed = 0
    for path in paths:
        try:
            case = load_fixture(path)
            messages = build_replay_messages(case)
            plan_messages = (
                turn_planner.build_turn_plan_messages(
                    case.session, case.module, case.player_char, case.events,
                    teammates=case.teammates or None,
                ) if case.events else []
            )
            total_chars = sum(len(m.get("content") or "") for m in messages)
            print(
                f"  ok  {case.name}: kp_messages={len(messages)} "
                f"({total_chars} chars), plan_messages={len(plan_messages)}, "
                f"events={len(case.events)}"
            )
        except Exception as exc:  # noqa: BLE001 —— smoke 就是要把问题全暴露出来
            failed += 1
            print(f"FAIL  {path.stem}: {type(exc).__name__}: {exc}")
    print(f"\nsmoke: {len(paths) - failed}/{len(paths)} 通过")
    return 1 if failed else 0


def _print_summary(results: list[dict]) -> None:
    print(f"\n{'fixture':<28} {'结果':<6} 明细")
    print("-" * 72)
    for r in results:
        errors = [f for f in r["findings"] if f["severity"] == "error"]
        warns = [f for f in r["findings"] if f["severity"] == "warn"]
        judge_failed = (
            [k for k, v in r["judge"].items() if not v["pass"]] if r["judge"] else []
        )
        detail_parts = []
        if errors:
            detail_parts.append(f"检查错误 {len(errors)}")
        if judge_failed:
            detail_parts.append(f"裁判不过: {','.join(judge_failed)}")
        if r["judge_error"]:
            detail_parts.append("裁判失败")
        if warns:
            detail_parts.append(f"警告 {len(warns)}")
        status = "PASS" if r["passed"] else "FAIL"
        print(f"{r['fixture']:<28} {status:<6} {'; '.join(detail_parts) or '-'}")
    passed = sum(1 for r in results if r["passed"])
    print("-" * 72)
    print(f"共 {len(results)} 个，通过 {passed}，不通过 {len(results) - passed}")


async def main_async(args: argparse.Namespace) -> int:
    paths = iter_fixtures(suite=args.suite, name=args.fixture)
    if not paths:
        print("没有匹配的 fixture。先用 python -m evals.snapshot 从真实会话导出。")
        return 1

    if args.smoke:
        return run_smoke(paths)

    llm = get_llm()
    if args.tool_loop and not llm.supports_tools():
        print("当前激活的 AI 配置不支持工具调用（supports_tools=False），无法 --tool-loop。")
        return 1
    results = []
    for path in paths:
        case = load_fixture(path)
        print(f"运行 {case.name} …", flush=True)
        try:
            results.append(await run_case(
                case, llm, use_judge=not args.no_judge, tool_loop=args.tool_loop,
            ))
        except Exception as exc:  # noqa: BLE001 —— 单个 fixture 失败不中断整套
            print(f"  出错: {type(exc).__name__}: {exc}")
            results.append({
                "fixture": case.name, "tags": case.tags, "plan_source": None,
                "plan": None, "narration": "", "findings": [],
                "judge": None, "judge_error": True, "passed": False,
                "run_error": f"{type(exc).__name__}: {exc}",
            })

    scorecard = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "model": _active_model_name(),
            "suite": args.suite,
            "judge": not args.no_judge,
            "tool_loop": args.tool_loop,
        },
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["passed"]),
        },
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{stamp}-{scorecard['meta']['git_sha']}.json"
    out.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(results)
    print(f"\nscorecard 已写入 {out}")
    return 0 if scorecard["summary"]["passed"] == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="重放 fixture 评估 KP 生成质量")
    parser.add_argument("--suite", help="只跑带此 tag 的 fixture")
    parser.add_argument("--fixture", help="只跑指定名字的 fixture")
    parser.add_argument("--no-judge", action="store_true", help="跳过裁判模型（只跑确定性检查）")
    parser.add_argument("--smoke", action="store_true", help="不调 LLM，只验证 fixture 可重建")
    parser.add_argument(
        "--tool-loop", dest="tool_loop", action="store_true",
        help="走 agent loop（工具调用）路径重放生成（需当前 Provider 支持工具）",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
