"""KP 沉浸感增强的回归测试。

Phase 1：场景 danger/atmosphere 注入与叙事调制指令。
Phase 2：模组 world_setting.intro 作为开场世界观导入。
均不依赖真实 LLM，只验证提示词/上下文的组装边界。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai import context as ctx
from app.models import (  # noqa: F401 — 注册全部表
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
)


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, scenes, world_setting=None):
    module = Module(
        title="氛围测试", rule_system="coc",
        scenes=scenes, npcs=[], clues=[],
        world_setting=world_setting or {},
    )
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id,
        status="active", current_scene_id=scenes[0]["id"],
    )
    db.add(session)
    db.commit()
    return module, hero, session


# ---------- Phase 1：场景 danger/atmosphere ----------

def test_current_scene_carries_danger_and_atmosphere(db_factory):
    """当前场景的 danger/atmosphere 应整块进入 KP 系统提示。"""
    db = db_factory()
    module, hero, session = _seed(db, scenes=[
        {"id": "crypt", "name": "墓室", "description": "潮湿的地下墓室",
         "danger": "deadly", "atmosphere": "腐臭、低压、随时塌方"},
    ])
    sys = ctx.build_kp_context(session, module, hero, [])[0]["content"]
    assert "deadly" in sys
    assert "腐臭、低压、随时塌方" in sys


def test_kp_prompt_has_atmosphere_modulation_instruction(db_factory):
    """系统提示应包含「依 danger/atmosphere 调制叙事」的通用指令，且要求不直白点明危险。"""
    db = db_factory()
    module, hero, session = _seed(db, scenes=[
        {"id": "hall", "name": "门厅", "description": "昏暗门厅", "danger": "uneasy", "atmosphere": "霉味"},
    ])
    sys = ctx.build_kp_context(session, module, hero, [])[0]["content"]
    assert "danger" in sys and "atmosphere" in sys
    # 危险靠氛围传达、绝不直白点明
    assert "绝不直白点明" in sys


def test_scene_without_danger_still_builds(db_factory):
    """缺省 danger/atmosphere 的旧模组场景仍能正常组装上下文（向后兼容）。"""
    db = db_factory()
    module, hero, session = _seed(db, scenes=[
        {"id": "room", "name": "房间", "description": "一个房间"},
    ])
    sys = ctx.build_kp_context(session, module, hero, [])[0]["content"]
    assert "房间" in sys


# ---------- Phase 2：world_setting.intro 开场世界观导入 ----------

def _opening_text(msgs):
    return "\n".join(m["content"] for m in msgs if m["role"] == "user")


def test_intro_injected_into_opening(db_factory):
    """有 intro 时，开场白第一拍注入世界观导入原文。"""
    db = db_factory()
    intro = "1923 年的新英格兰，雾季漫长，这是一个体面人家背后腐烂之物的缓燃恐怖故事。"
    module, hero, session = _seed(
        db,
        scenes=[{"id": "hall", "name": "门厅", "description": "昏暗门厅"}],
        world_setting={"intro": intro},
    )
    opening = _opening_text(ctx.build_kp_context(session, module, hero, []))
    assert intro in opening
    assert "世界观导入" in opening


def test_no_intro_no_induction(db_factory):
    """无 intro 时不强加世界观导入拍，开场退回纯场景钩子。"""
    db = db_factory()
    module, hero, session = _seed(
        db,
        scenes=[{"id": "hall", "name": "门厅", "description": "昏暗门厅"}],
        world_setting={},
    )
    opening = _opening_text(ctx.build_kp_context(session, module, hero, []))
    assert "世界观导入" not in opening
    assert "把玩家带入起始场景" in opening


def test_intro_not_leaked_during_play(db_factory):
    """游戏开始后（已有事件）不再重复世界观导入拍。"""
    db = db_factory()
    module, hero, session = _seed(
        db,
        scenes=[{"id": "hall", "name": "门厅", "description": "昏暗门厅"}],
        world_setting={"intro": "某段世界观铺陈"},
    )
    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="开场已生成", actor_name="KP")
    opening = _opening_text(ctx.build_kp_context(session, module, hero, [ev]))
    assert "世界观导入" not in opening


def test_create_module_persists_intro(db_factory):
    """create_module 把顶层 intro 落进 world_setting（与 player_brief 同处）。"""
    from app.services.module_service import create_module
    db = db_factory()
    module = create_module(db, {
        "title": "导入测试", "rule_system": "coc",
        "intro": "一段世界观铺陈", "player_brief": "你受托前来",
        "scenes": [], "npcs": [], "clues": [],
    })
    assert module.world_setting["intro"] == "一段世界观铺陈"
    assert module.world_setting["player_brief"] == "你受托前来"


# ---------- 剧情状态机（方案 A）：flags + 场景/NPC 状态变体 ----------

def _seed_stateful(db, *, scenes, npcs, flags=None):
    module = Module(title="状态测试", rule_system="coc", scenes=scenes, npcs=npcs, clues=[])
    hero = Character(name="调查员", rule_system="coc", is_player=True)
    db.add_all([module, hero])
    db.commit()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id,
        status="active", current_scene_id=scenes[0]["id"],
        world_state={"flags": flags or {}, "visited_scenes": [scenes[0]["id"]]},
    )
    db.add(session)
    db.commit()
    return module, hero, session


def test_scene_variant_overrides_when_flag_active(db_factory):
    """flag 激活后，当前场景采用命中变体的 danger/atmosphere。"""
    db = db_factory()
    scene = {
        "id": "basement", "name": "地下室", "description": "干燥的地下室",
        "danger": "calm", "atmosphere": "霉味",
        "states": [{"when": ["basement_flooded"], "danger": "deadly",
                    "atmosphere": "齐腰黑水、电线垂落", "description": "灌满黑水的地下室"}],
    }
    module, hero, session = _seed_stateful(
        db, scenes=[scene], npcs=[], flags={"basement_flooded": True},
    )
    # 已有事件 -> 走运行时分支（当前场景整块进提示）
    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="进行中", actor_name="KP")
    sys = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "deadly" in sys and "齐腰黑水" in sys
    assert "灌满黑水的地下室" in sys


def test_scene_default_when_flag_inactive(db_factory):
    """flag 未激活时，场景回到默认状态（向后兼容）。"""
    db = db_factory()
    scene = {
        "id": "basement", "name": "地下室", "description": "干燥的地下室",
        "danger": "calm", "atmosphere": "霉味",
        "states": [{"when": ["basement_flooded"], "danger": "deadly", "atmosphere": "齐腰黑水"}],
    }
    module, hero, session = _seed_stateful(db, scenes=[scene], npcs=[], flags={})
    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="进行中", actor_name="KP")
    sys = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "霉味" in sys
    assert "齐腰黑水" not in sys


def test_npc_variant_and_plot_state_block(db_factory):
    """NPC 死亡变体被标注；剧情状态区列出已激活标志。"""
    db = db_factory()
    npc = {
        "id": "butler", "name": "管家", "description": "恭顺的老管家",
        "personality": "谦卑", "initial_location": "basement",
        "states": [{"when": ["butler_dead"], "alive": False}],
    }
    scene = {"id": "basement", "name": "地下室", "description": "地下室"}
    module, hero, session = _seed_stateful(
        db, scenes=[scene], npcs=[npc], flags={"butler_dead": True},
    )
    ev = EventLog(session_id=session.id, sequence_num=1, event_type="narration",
                  content="进行中", actor_name="KP")
    sys = ctx.build_kp_context(session, module, hero, [ev])[0]["content"]
    assert "已死亡" in sys              # NPC 变体生效
    assert "butler_dead" in sys         # 剧情状态区列出激活标志


def test_no_flags_plot_state_is_neutral(db_factory):
    """无任何 flag 时剧情状态区给中性占位，不污染叙事。"""
    db = db_factory()
    module, hero, session = _seed_stateful(
        db, scenes=[{"id": "room", "name": "房间", "description": "房间"}], npcs=[], flags={},
    )
    sys = ctx.build_kp_context(session, module, hero, [])[0]["content"]
    assert "暂无特殊剧情标志" in sys
