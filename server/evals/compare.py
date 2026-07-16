"""对比两次 scorecard：prompt 改动前后各 fixture 的通过状态变化。

用法：python -m evals.compare results/A.json results/B.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _index(scorecard: dict) -> dict[str, dict]:
    return {r["fixture"]: r for r in scorecard.get("results") or []}


# 会追踪逐项 diff 的 error 级确定性检查（warn 级只提示、不计 diff）。
_TRACKED_CHECKS = ("internal_ids", "report_style", "command_syntax", "event_echo", "plan_adjudication")


def _samples(result: dict) -> list[dict]:
    """统一取样本列表：新形态用 samples[]；旧扁平形态把结果自身当作单次样本（向后兼容）。"""
    return result.get("samples") or [result]


def _pass_rate(result: dict) -> float:
    if "pass_rate" in result:
        return float(result["pass_rate"])
    return 1.0 if result.get("passed") else 0.0


def _item_states(result: dict) -> dict[str, float]:
    """展开成 {检查/评分项: 通过率∈[0,1]}——跨采样聚合，便于逐项 diff（单次采样即 0/1）。"""
    samples = _samples(result)
    n = len(samples) or 1
    states: dict[str, float] = {}
    for check in _TRACKED_CHECKS:
        ok = sum(
            1 for s in samples
            if check not in {
                f["check"] for f in (s.get("findings") or []) if f["severity"] == "error"
            }
        )
        states[f"check:{check}"] = ok / n
    judge_keys = {k for s in samples for k in (s.get("judge") or {})}
    for key in judge_keys:
        ok = sum(1 for s in samples if (s.get("judge") or {}).get(key, {}).get("pass"))
        states[f"judge:{key}"] = ok / n
    return states


def main() -> None:
    parser = argparse.ArgumentParser(description="对比两次 scorecard")
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    args = parser.parse_args()

    old_card = json.loads(args.old.read_text(encoding="utf-8"))
    new_card = json.loads(args.new.read_text(encoding="utf-8"))
    old_results, new_results = _index(old_card), _index(new_card)

    print(f"旧: {args.old.name}  (模型 {old_card['meta'].get('model')}, "
          f"sha {old_card['meta'].get('git_sha')})")
    print(f"新: {args.new.name}  (模型 {new_card['meta'].get('model')}, "
          f"sha {new_card['meta'].get('git_sha')})\n")

    def _rate_str(r: dict) -> str:
        return f"{_pass_rate(r) * 100:.0f}%" + (f"（{r['pass_count']}/{r['runs']}）" if "runs" in r else "")

    regressed = improved = 0
    for name in sorted(set(old_results) | set(new_results)):
        if name not in old_results:
            print(f"{name}: 新增 fixture（通过率 {_rate_str(new_results[name])}）")
            continue
        if name not in new_results:
            print(f"{name}: 本次未运行")
            continue
        # 逐项通过率 diff（跨采样聚合后比较；> 一个采样步长才算变化，避免单次抖动误报）
        old_states, new_states = _item_states(old_results[name]), _item_states(new_results[name])
        eps = 1e-9
        changes = []
        for key in sorted(set(old_states) | set(new_states)):
            before, after = old_states.get(key, 1.0), new_states.get(key, 1.0)
            if after < before - eps:
                changes.append(f"{key} {before * 100:.0f}%→{after * 100:.0f}%（回退）")
                regressed += 1
            elif after > before + eps:
                changes.append(f"{key} {before * 100:.0f}%→{after * 100:.0f}%（改善）")
                improved += 1
        rate_before, rate_after = _pass_rate(old_results[name]), _pass_rate(new_results[name])
        if changes or abs(rate_after - rate_before) > eps:
            print(f"{name}: 整体通过率 {rate_before * 100:.0f}%→{rate_after * 100:.0f}%")
            for c in changes:
                print(f"    {c}")

    old_sum, new_sum = old_card.get("summary") or {}, new_card.get("summary") or {}

    def _sum_str(s: dict) -> str:
        # 新形态：fully_passed / total（+ 均通过率）；旧扁平形态：passed / total
        if "fully_passed" in s:
            mean = s.get("mean_pass_rate")
            tail = f"，均通过率 {mean * 100:.0f}%" if isinstance(mean, (int, float)) else ""
            return f"稳过 {s.get('fully_passed')}/{s.get('total')}{tail}"
        return f"{s.get('passed')}/{s.get('total')}"

    print(f"\n总体：{_sum_str(old_sum)} → {_sum_str(new_sum)}"
          f"（逐项改善 {improved}，回退 {regressed}）")
    sys.exit(1 if regressed else 0)


if __name__ == "__main__":
    main()
