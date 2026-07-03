"""_acting_player_names：本轮行动者判定（多人局杜绝替未行动玩家编动作）。"""

from types import SimpleNamespace

from app.ai.context import _acting_player_names

PARTY = ["詹姆斯·卡特", "哈桑·艾哈迈德"]


def _ev(etype, actor="", seq=0):
    return SimpleNamespace(event_type=etype, actor_name=actor, sequence_num=seq)


def test_只取末尾连续玩家行动():
    # 上一轮 KP narration 之后，只有詹姆斯行动 → 本轮行动者只有詹姆斯
    events = [
        _ev("action", "哈桑·艾哈迈德", 1),
        _ev("narration", "KP", 2),
        _ev("dice", "系统", 3),
        _ev("action", "詹姆斯·卡特", 4),
    ]
    assert _acting_player_names(events, PARTY) == ["詹姆斯·卡特"]


def test_多个玩家同轮都行动保持顺序():
    events = [
        _ev("narration", "KP", 1),
        _ev("dialogue", "詹姆斯·卡特", 2),
        _ev("action", "哈桑·艾哈迈德", 3),
    ]
    assert _acting_player_names(events, PARTY) == ["詹姆斯·卡特", "哈桑·艾哈迈德"]


def test_ooc跳过不中断():
    events = [
        _ev("narration", "KP", 1),
        _ev("action", "詹姆斯·卡特", 2),
        _ev("ooc", "哈桑·艾哈迈德", 3),
    ]
    assert _acting_player_names(events, PARTY) == ["詹姆斯·卡特"]


def test_dice或system中断本轮():
    events = [
        _ev("action", "詹姆斯·卡特", 1),
        _ev("system", "系统", 2),   # 上一轮的检定提示 → 中断
        _ev("dialogue", "哈桑·艾哈迈德", 3),
    ]
    assert _acting_player_names(events, PARTY) == ["哈桑·艾哈迈德"]


def test_非队伍名不计():
    events = [_ev("dialogue", "守墓人", 1), _ev("action", "詹姆斯·卡特", 2)]
    assert _acting_player_names(events, PARTY) == ["詹姆斯·卡特"]


def test_空事件():
    assert _acting_player_names([], PARTY) == []
