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


def _item_states(result: dict) -> dict[str, bool]:
    """展开成 {检查/评分项: 是否通过}，便于逐项 diff。"""
    states: dict[str, bool] = {}
    error_checks = {
        f["check"] for f in result.get("findings") or [] if f["severity"] == "error"
    }
    for check in ("internal_ids", "report_style", "command_syntax"):
        states[f"check:{check}"] = check not in error_checks
    for key, v in (result.get("judge") or {}).items():
        states[f"judge:{key}"] = bool(v.get("pass"))
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

    regressed = improved = 0
    for name in sorted(set(old_results) | set(new_results)):
        if name not in old_results:
            print(f"{name}: 新增 fixture（{'PASS' if new_results[name]['passed'] else 'FAIL'}）")
            continue
        if name not in new_results:
            print(f"{name}: 本次未运行")
            continue
        old_states, new_states = _item_states(old_results[name]), _item_states(new_results[name])
        changes = []
        for key in sorted(set(old_states) | set(new_states)):
            before, after = old_states.get(key), new_states.get(key)
            if before is True and after is False:
                changes.append(f"{key} 通过→不过")
                regressed += 1
            elif before is False and after is True:
                changes.append(f"{key} 不过→通过")
                improved += 1
        if changes:
            print(f"{name}:")
            for c in changes:
                print(f"    {c}")

    old_sum, new_sum = old_card.get("summary") or {}, new_card.get("summary") or {}
    print(f"\n总体：{old_sum.get('passed')}/{old_sum.get('total')} → "
          f"{new_sum.get('passed')}/{new_sum.get('total')}（改善 {improved} 项，回退 {regressed} 项）")
    sys.exit(1 if regressed else 0)


if __name__ == "__main__":
    main()
