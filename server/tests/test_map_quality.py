"""地图生成质量校验：大型室内「空荡大盒子」检测与带强化提示的重试。"""

import asyncio
import json

import pytest

from app.services import map_service


def _box_map(w=14, h=11):
    """一个只有外围墙、内部全空地板的大盒子（可走面积 (w-2)*(h-2)）。"""
    tiles = ["#" * w] + ["#" + "." * (w - 2) + "#" for _ in range(h - 2)] + ["#" * w]
    return {"w": w, "h": h, "tiles": tiles, "objects": [], "entrances": [], "npc_pos": []}


def _partitioned_map(w=14, h=11):
    """同尺寸但中间加了一道带门的隔断墙。"""
    m = _box_map(w, h)
    mid = h // 2
    row = "#" + "#" * (w - 2) + "#"
    row = row[:6] + "+" + row[7:]   # 隔断墙上开一扇门
    m["tiles"][mid] = row
    return m


def test_validate_flags_big_empty_box():
    issues = map_service.validate_map(_box_map())
    assert any(map_service._NO_PARTITION_ISSUE in s for s in issues)


def test_validate_accepts_partitioned():
    issues = map_service.validate_map(_partitioned_map())
    assert not any(map_service._NO_PARTITION_ISSUE in s for s in issues)


def test_validate_skips_outdoor_and_small():
    # 室外（无墙）：大片空地不算「室内大盒子」
    outdoor = {"w": 14, "h": 11, "tiles": ["." * 14 for _ in range(11)],
               "objects": [], "entrances": [], "npc_pos": []}
    assert not any(map_service._NO_PARTITION_ISSUE in s for s in map_service.validate_map(outdoor))
    # 小房间：面积不足阈值不受约束
    small = _box_map(7, 6)
    assert not any(map_service._NO_PARTITION_ISSUE in s for s in map_service.validate_map(small))


def test_generate_retries_once_on_empty_box(monkeypatch):
    """首次生成空荡大盒子 → 带强化提示重试一次并采用重试结果。"""
    calls: list[str] = []

    class _LLM:
        async def complete(self, messages, **kw):
            calls.append(messages[0]["content"])
            if len(calls) == 1:
                return json.dumps(_box_map(), ensure_ascii=False)
            return json.dumps(_partitioned_map(), ensure_ascii=False)

    monkeypatch.setattr(map_service, "get_llm", lambda: _LLM())
    scene = {"id": "s1", "name": "疗养院", "description": "大型机构", "connections": []}
    out = asyncio.run(map_service.generate_scene_map(scene, [], [], {}))
    assert len(calls) == 2
    assert "返工要求" in calls[1]                       # 重试带了强化提示
    assert not any(map_service._NO_PARTITION_ISSUE in s for s in (out.get("_issues") or []))
    assert "+" in out["tiles"][5]                       # 用的是重试的隔断版


def test_generate_no_retry_when_partitioned(monkeypatch):
    calls: list[str] = []

    class _LLM:
        async def complete(self, messages, **kw):
            calls.append(messages[0]["content"])
            return json.dumps(_partitioned_map(), ensure_ascii=False)

    monkeypatch.setattr(map_service, "get_llm", lambda: _LLM())
    scene = {"id": "s1", "name": "疗养院", "description": "大型机构", "connections": []}
    asyncio.run(map_service.generate_scene_map(scene, [], [], {}))
    assert len(calls) == 1


def test_prompt_contains_partition_and_exit_semantics():
    prompt = map_service.build_map_prompt(
        {"id": "s1", "name": "疗养院", "description": "机构", "connections": []}, [], [], {},
    )
    assert "房间分割" in prompt
    assert "出口语义" in prompt
