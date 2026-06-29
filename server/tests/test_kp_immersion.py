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
