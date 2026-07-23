"""真人 KP + 全 AI 玩家席（KP 独走局）：席位自由组合、开局门槛、推进链锚点与信号。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Character, GameSession, Module
from app.services import human_kp_service, session_service


def _db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kp-all-ai.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db):
    module = Module(
        title="独走测试", description="真人 KP 全 AI 局。", rule_system="coc",
        scenes=[{"id": "s1", "title": "门厅"}], npcs=[],
    )
    a1 = Character(name="铁手阿甘", rule_system="coc", is_player=False, skills={"侦查": 50})
    a2 = Character(name="书记员温妮", rule_system="coc", is_player=False, skills={"图书馆": 70})
    db.add_all([module, a1, a2])
    db.commit()
    return module, a1, a2


def _all_ai_session(db, module, a1, a2) -> GameSession:
    return session_service.create_session(
        db, module.id,
        [
            {"character_id": a1.id, "role": "ai", "is_primary": True},
            {"character_id": a2.id, "role": "ai"},
        ],
        creator_token="kp-token", kp_mode="human",
    )


def test_全AI玩家席建局主角保持AI且非legacy(tmp_path):
    db = _db(tmp_path)()
    module, a1, a2 = _seed(db)
    session = _all_ai_session(db, module, a1, a2)
    parts = session_service.get_participants(db, session.id)
    primary = next(p for p in parts if p.is_primary)
    assert primary.role == "ai" and primary.character_id == a1.id   # 主角锚点允许是 AI
    assert session.identity_version == 2                            # 不被误判为 legacy 双席位
    assert session.player_character_id == a1.id
    assert session.status == "active"                               # 无空真人席 → 直接开局
    kp = next(p for p in parts if p.role == "kp")
    assert kp.owner_token == "kp-token"
    assert session_service.is_kp(db, session.id, "kp-token")


def test_真人KP局开局门槛不再要求真人玩家(tmp_path):
    db = _db(tmp_path)()
    module, a1, a2 = _seed(db)
    session = _all_ai_session(db, module, a1, a2)
    assert session_service.lobby_gaps(db, session.id) == []          # 全 AI 已入座 → 可开局

    # 零角色（只有留空真人席）仍要拦：不能开一个没有任何角色的局
    empty = session_service.create_session(
        db, module.id, [{"character_id": None, "role": "human", "is_primary": True}],
        creator_token="kp-2", kp_mode="human",
    )
    gaps = session_service.lobby_gaps(db, empty.id)
    assert any("至少需要 1 个已入座的角色" in g for g in gaps)
    assert all("真人玩家" not in g for g in gaps)                    # 不再要求真人玩家


def test_AI模式仍要求至少一名真人玩家(tmp_path):
    db = _db(tmp_path)()
    module, a1, _a2 = _seed(db)
    hero = Character(name="真人英雄", rule_system="coc", is_player=True)
    db.add(hero); db.commit()
    session = session_service.create_session(
        db, module.id, [{"character_id": hero.id, "role": "human", "is_primary": True}],
        creator_token="p1", kp_mode="ai",
    )
    # AI KP 模式的既有约束不变：主角强制真人、有真人才可开局
    primary = next(p for p in session_service.get_participants(db, session.id) if p.is_primary)
    assert primary.role == "human"
    assert session_service.lobby_gaps(db, session.id) == []


def test_上下文锚点回落到AI队友(tmp_path):
    db = _db(tmp_path)()
    module, a1, a2 = _seed(db)
    # 空真人主角席 + 已填 AI 席：player_character_id 为空，锚点应回落到 AI 队友
    session = session_service.create_session(
        db, module.id,
        [
            {"character_id": None, "role": "human", "is_primary": True},
            {"character_id": a1.id, "role": "ai"},
        ],
        creator_token="kp-token", kp_mode="human",
    )
    assert session.player_character_id is None
    anchor = human_kp_service.resolve_player_character(db, session.id, session)
    assert anchor is not None and anchor.id == a1.id


def test_零真人时KP旁白驱动队友回合信号(tmp_path):
    db = _db(tmp_path)()
    module, a1, a2 = _seed(db)
    session = _all_ai_session(db, module, a1, a2)
    # 无任何事件 → 无信号
    assert human_kp_service.current_player_turn_marker(db, session.id) == 0
    assert not human_kp_service.has_unprocessed_player_turn(db, session.id, session)
    # KP 发布一段旁白 → 出现新信号，可推进一轮 AI 队友
    ev = session_service.add_event(db, session.id, "narration", "灯火在门厅尽头摇晃。",
                                   actor_name="KP")
    assert human_kp_service.current_player_turn_marker(db, session.id) == ev.sequence_num
    assert human_kp_service.has_unprocessed_player_turn(db, session.id, session)
    # 队友回合落账后（last_ai_team_turn_seq 对齐）→ 同一段旁白不重复推进
    state = dict(session.kp_state or {})
    state["last_ai_team_turn_seq"] = ev.sequence_num
    session.kp_state = state
    db.add(session); db.commit(); db.refresh(session)
    assert not human_kp_service.has_unprocessed_player_turn(db, session.id, session)
    # AI 队友自己的行动事件（action）不构成新信号——不会自触发循环
    session_service.add_event(db, session.id, "action", "阿甘检查了门闩。",
                              actor_id=a1.id, actor_name=a1.name)
    assert not human_kp_service.has_unprocessed_player_turn(db, session.id, session)
