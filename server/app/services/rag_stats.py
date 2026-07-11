"""某局游戏的 RAG（规则书 / 模组原文检索）调用统计。

落在 ``world_state.rag_stats``，供后台评估 RAG 到底被用了多少、命中质量如何——
判断这套检索对跑团的实际帮助。纯读改写、fail-open，绝不影响生成主流程。

统计维度：kind(rule|module) × mode(active|passive) 四象限的调用次数、命中行数、
空命中次数与 top 命中分累计；另滚动保留最近若干次检索样本供人工核查。
- active：KP 主动发起 [RULE_LOOKUP]/[MODULE_LOOKUP]（tool 环与旧路径都收敛到此）；
- passive：建 KP 上下文时按 turn_kind/场景预取的规则要点、模组原文摘录。
"""

from __future__ import annotations

_MAX_SAMPLES = 30  # 最近检索样本上限（滚动保留，供人工核查命中质量）


def _blank() -> dict:
    return {"totals": {"calls": 0, "hits": 0, "empty": 0}, "by_kind_mode": {}, "recent": []}


def record(ws: dict, *, kind: str, mode: str, query: str, hits: list | None) -> dict:
    """把一次 RAG 检索并入统计并返回新的 world_state（浅拷贝，不原地改旧引用）。

    ``hits`` 为检索返回的命中列表（元素含 ``score``）；空命中也记（计入 empty，
    用于看「查了却没查到」的比例——空命中多说明语料覆盖或 query 组织有问题）。
    """
    stats = ws.get("rag_stats") or _blank()
    totals = dict(stats.get("totals") or {"calls": 0, "hits": 0, "empty": 0})
    by_km = dict(stats.get("by_kind_mode") or {})
    recent = list(stats.get("recent") or [])

    n = len(hits or [])
    top = max((float(h.get("score") or 0.0) for h in (hits or [])), default=0.0)

    key = f"{kind}:{mode}"
    slot = dict(by_km.get(key) or {"calls": 0, "hits": 0, "empty": 0, "score_sum": 0.0})
    slot["calls"] += 1
    if n:
        slot["hits"] += n
        slot["score_sum"] = round((slot.get("score_sum") or 0.0) + top, 4)
    else:
        slot["empty"] += 1
    by_km[key] = slot

    totals["calls"] += 1
    totals["hits"] += n
    if not n:
        totals["empty"] += 1

    sample = {"kind": kind, "mode": mode, "query": (query or "")[:80],
              "n_hits": n, "top_score": round(top, 4)}
    recent = ([sample] + recent)[:_MAX_SAMPLES]

    new_ws = dict(ws)
    new_ws["rag_stats"] = {"totals": totals, "by_kind_mode": by_km, "recent": recent}
    return new_ws


def summarize(ws: dict | None) -> dict:
    """把累计统计整理成可读汇总：四象限计数 + 命中率 + 平均 top 命中分 + 最近样本。"""
    stats = (ws or {}).get("rag_stats") or _blank()
    quadrants = {}
    for key, slot in (stats.get("by_kind_mode") or {}).items():
        calls = slot.get("calls") or 0
        hit_calls = calls - (slot.get("empty") or 0)
        quadrants[key] = {
            "calls": calls,
            "empty": slot.get("empty") or 0,
            "total_hits": slot.get("hits") or 0,
            "hit_rate": round(hit_calls / calls, 4) if calls else 0.0,
            "avg_top_score": round((slot.get("score_sum") or 0.0) / hit_calls, 4) if hit_calls else 0.0,
        }
    totals = stats.get("totals") or {}
    calls = totals.get("calls") or 0
    empty = totals.get("empty") or 0
    return {
        "totals": {
            "calls": calls,
            "total_hits": totals.get("hits") or 0,
            "empty": empty,
            "hit_rate": round((calls - empty) / calls, 4) if calls else 0.0,
        },
        "by_kind_mode": quadrants,
        "recent": list(stats.get("recent") or []),
    }
