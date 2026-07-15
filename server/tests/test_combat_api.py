"""战斗反应端点（POST /combat/reaction）+ GET /combat 带 pending_reaction 的 HTTP 层回归。

用 TestClient + get_db 依赖覆盖，临时 SQLite；掷骰经 monkeypatch 钉死，不触达真实 LLM。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import get_db
from app.main import app
from app.models import (  # noqa: F401 — 注册全部表
    Base,
    Character,
    EventLog,
    GameSession,
    Module,
    SessionParticipant,
)
from app.services import combat_service


def _seq(values):
    it = iter(values)
    return lambda: next(it)


def _fix_rolls(monkeypatch, d100_seq, die=3):
    monkeypatch.setattr("app.rules.coc.checks.roll_percentile", _seq(d100_seq))
    monkeypatch.setattr("app.rules.coc.combat.random.randint", lambda a, b: die)


@pytest.fixture
def env(tmp_path):
    """临时库 + 一局停在「NPC 攻击真人」暂停点（pending_reaction 已置）的战斗。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'combat_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    import asyncio

    db = TestingSession()
    module = Module(title="M", rule_system="coc", npcs=[], scenes=[])
    hero = Character(
        name="伊芙琳", rule_system="coc", is_player=True,
        base_attributes={"DEX": 70, "CON": 60, "SIZ": 50},
        skills={"格斗(斗殴)": 60, "闪避": 35},
        system_data={"hitPoints": {"current": 11, "max": 11}, "damageBonus": "0"},
    )
    db.add_all([module, hero])
    db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id, status="active", world_state={},
    )
    db.add(session)
    db.commit()
    sid, hero_id = session.id, hero.id

    # 建战斗态并驱动到「打手（先攻首位）攻击真人」暂停点 → pending_reaction 已置。
    enemy = {"id": "npc_thug", "name": "打手",
             "attributes": {"DEX": 90, "CON": 50, "SIZ": 60},
             "skills": {"格斗(斗殴)": 45, "闪避": 20}, "weapon": "徒手格斗"}
    state = combat_service.start_combat(
        db, sid,
        [combat_service._char_participant(hero, "player", is_human=True)],
        [combat_service._npc_participant(enemy, "enemy")])
    # 摆到相邻：拉开布阵下 NPC 会先走位接近，这里让打手开局就够得着 hero → 直接攻击暂停
    combat_service._find(state, hero.id)["pos"] = {"x": 5, "y": 5}
    combat_service._find(state, "npc_thug")["pos"] = {"x": 6, "y": 5}
    combat_service._save_combat(db, sid, state)
    asyncio.run(combat_service.drive_npcs(db, sid, state))
    assert state.get("pending_reaction")   # 已停在等真人反应
    db.close()

    yield TestClient(app), sid, hero_id
    app.dependency_overrides.clear()


def test_get_combat_carries_pending_reaction(env):
    """断线重连恢复：GET /combat 带上 pending_reaction 供前端恢复反应提示。"""
    c, sid, hero_id = env
    body = c.get(f"/api/sessions/{sid}/combat").json()
    assert body["active"] is True
    pr = body["pending_reaction"]
    assert pr and pr["defender_id"] == hero_id and pr["attacker_id"] == "npc_thug"
    assert pr["allowed"] == ["fight_back", "dodge"]
    # 重连恢复也要带上名字，否则前端提示渲染成「undefined 用 X 攻击你」
    assert pr["attacker_name"] and pr["defender_name"]


def test_post_reaction_resolves_and_clears_pending(env, monkeypatch):
    """真人经端点提交闪避 → 200，结算这一击并清空 pending_reaction。"""
    c, sid, hero_id = env
    # 攻方掷 80（>45 失手）、真人闪避掷 10（<35 成功）→ 未命中；无 token → 回落主角=防御者。
    _fix_rolls(monkeypatch, [80, 10], die=3)
    resp = c.post(f"/api/sessions/{sid}/combat/reaction", json={"choice": "dodge"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    # pending 已清空（resolve_reaction 续跑后广播新 combat_state）。
    after = c.get(f"/api/sessions/{sid}/combat").json()
    assert after.get("pending_reaction") is None


def test_post_reaction_disallowed_choice_returns_409(env):
    """不在 allowed 里的反应（徒手攻击下选 cover）→ 409。"""
    c, sid, _ = env
    resp = c.post(f"/api/sessions/{sid}/combat/reaction", json={"choice": "cover"})
    assert resp.status_code == 409, resp.text
