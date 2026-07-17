"""临时疯狂『一阵疯狂』症状：进入时掷症状、影响检定（惩罚骰）与言行（上下文注入）、1D10 回合后解除。"""


import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.context import _format_player_info
from app.models import Base, Character, GameSession, Module  # noqa: F401
from app.rules.coc import madness as m
from app.services import chat_service as cs


@pytest.fixture
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mad.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, san=50):
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    pc = Character(name="山田", rule_system="coc", is_player=True, status="active",
                   base_attributes={}, skills={},
                   system_data={"sanity": {"current": san, "max": 99}})
    db.add_all([module, pc]); db.flush()
    s = GameSession(module_id=module.id, player_character_id=pc.id, status="active", world_state={})
    db.add(s); db.commit()
    return s.id, pc


# ── 规则表 ──

def test_症状表与惩罚骰域匹配():
    assert len(m.BOUT_SYMPTOMS) == 10
    bout = m.make_bout()
    assert bout["symptom"] and bout["label"] and 1 <= bout["turns_left"] <= 10
    paranoia = next(s for s in m.BOUT_SYMPTOMS if s["key"] == "paranoia")
    assert m.check_penalty(paranoia, "话术") == 1        # 命中 penalty_skills
    assert m.check_penalty(paranoia, "力量") == 0        # 不相关技能不罚
    assert m.check_penalty(None, "话术") == 0


# ── 进入疯狂掷症状 ──

def test_临时疯狂落库掷症状(db_factory):
    db = db_factory(); _, pc = _seed(db)
    status = cs._apply_madness_status(db, pc, new_san=10, went_insane=True)
    assert status == "temporary_insanity" and pc.status == "temporary_insanity"
    bout = pc.system_data["madness"]
    assert bout["symptom"] in {s["key"] for s in m.BOUT_SYMPTOMS}
    assert 1 <= bout["turns_left"] <= 10


def test_永久疯狂不掷一阵症状(db_factory):
    db = db_factory(); _, pc = _seed(db, san=0)
    status = cs._apply_madness_status(db, pc, new_san=0, went_insane=True)
    assert status == "permanent_insanity" and "madness" not in (pc.system_data or {})


# ── 言行影响：上下文注入 ──

def test_上下文注入症状与接管提示(db_factory):
    db = db_factory(); _, pc = _seed(db)
    cs._apply_madness_status(db, pc, new_san=10, went_insane=True)
    # 钉成「暴力冲动」（incapacitated）以验证系统接管提示
    sd = dict(pc.system_data); sd["madness"] = m.make_bout()
    sd["madness"].update({"label": "暴力冲动", "manifest": "无差别攻击", "incapacitated": True})
    pc.system_data = sd; db.add(pc); db.commit()
    info = _format_player_info(pc)
    assert "临时疯狂·暴力冲动" in info and "无差别攻击" in info
    assert "无法正常自主行动" in info   # 系统接管提示


# ── 1D10 回合后解除 ──

def test_到期解除并广播恢复(db_factory):
    db = db_factory(); sid, pc = _seed(db)
    sd = dict(pc.system_data)
    sd["madness"] = {"symptom": "paranoia", "label": "偏执妄想", "manifest": "疑神疑鬼",
                     "penalty_skills": ["话术"], "incapacitated": False, "override": "", "turns_left": 1}
    pc.system_data = sd; pc.status = "temporary_insanity"; db.add(pc); db.commit()

    out = cs._tick_madness_recovery(db, sid, [pc])   # turns_left 1→0 → 解除
    assert any("恢复了神智" in c for c in out)
    assert pc.status == "active" and "madness" not in (pc.system_data or {})


def test_未到期只递减不解除(db_factory):
    db = db_factory(); sid, pc = _seed(db)
    sd = dict(pc.system_data)
    sd["madness"] = {"symptom": "paranoia", "label": "偏执", "manifest": "x",
                     "penalty_skills": [], "incapacitated": False, "override": "", "turns_left": 3}
    pc.system_data = sd; pc.status = "temporary_insanity"; db.add(pc); db.commit()

    out = cs._tick_madness_recovery(db, sid, [pc])
    assert out == []
    assert pc.status == "temporary_insanity" and pc.system_data["madness"]["turns_left"] == 2
