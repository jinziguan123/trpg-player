"""会话 / 角色 API 的 HTTP 层回归（含多席位序列化与 available 过滤）。

用 TestClient + get_db 依赖覆盖，临时 SQLite，不触达真实 LLM。
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


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}",
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

    db = TestingSession()
    module = Module(title="测试模组", rule_system="coc", npcs=[], scenes=[])
    hero = Character(name="主角", rule_system="coc", is_player=True)
    ally = Character(name="AI队友", rule_system="coc", is_player=False)
    db.add_all([module, hero, ally])
    db.commit()
    ids = {"module": module.id, "hero": hero.id, "ally": ally.id}
    db.close()

    yield TestClient(app), ids
    app.dependency_overrides.clear()


def test_create_session_with_participants_roundtrip(client):
    c, ids = client
    resp = c.post(
        "/api/sessions",
        json={
            "module_id": ids["module"],
            "participants": [
                {"character_id": ids["hero"], "role": "human", "is_primary": True},
                {"character_id": ids["ally"], "role": "ai", "is_primary": False},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_character_id"] == ids["hero"]
    parts = {p["character_id"]: p for p in body["participants"]}
    assert parts[ids["hero"]]["role"] == "human" and parts[ids["hero"]]["is_primary"]
    assert parts[ids["ally"]]["role"] == "ai"
    assert parts[ids["ally"]]["character_name"] == "AI队友"

    # get_session 也带参与者与名字
    sid = body["id"]
    got = c.get(f"/api/sessions/{sid}").json()
    assert len(got["participants"]) == 2
    assert any(p["character_name"] == "主角" for p in got["participants"])


def test_available_filter_excludes_occupied_and_respects_is_player(client):
    c, ids = client
    # 开局占用 hero + ally
    c.post(
        "/api/sessions",
        json={
            "module_id": ids["module"],
            "participants": [
                {"character_id": ids["hero"], "is_primary": True},
                {"character_id": ids["ally"], "role": "ai"},
            ],
        },
    )
    # available 的主角池应排除已占用的 hero
    heroes = c.get("/api/characters?available=true&is_player=true").json()
    assert ids["hero"] not in {h["id"] for h in heroes}
    # available 的队友池应排除已占用的 ally（参与表对齐）
    allies = c.get("/api/characters?available=true&is_player=false").json()
    assert ids["ally"] not in {a["id"] for a in allies}


def test_legacy_single_player_create_still_works(client):
    c, ids = client
    resp = c.post(
        "/api/sessions",
        json={"module_id": ids["module"], "player_character_id": ids["hero"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_character_id"] == ids["hero"]
    assert len(body["participants"]) == 1
    assert body["participants"][0]["is_primary"]


def _make_session(c, ids) -> str:
    resp = c.post(
        "/api/sessions",
        json={"module_id": ids["module"],
              "participants": [{"character_id": ids["hero"], "is_primary": True}]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_locations_endpoint_smoke(client):
    c, ids = client
    sid = _make_session(c, ids)
    r = c.get(f"/api/sessions/{sid}/locations")
    assert r.status_code == 200, r.text
    assert "locations" in r.json()


def test_rag_stats_endpoint_smoke(client):
    """RAG 统计端点：新会话返回零值汇总（不 500）。"""
    c, ids = client
    sid = _make_session(c, ids)
    r = c.get(f"/api/sessions/{sid}/rag-stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["calls"] == 0
    assert body["by_kind_mode"] == {} and body["recent"] == []


def test_travel_unknown_scene_rejected(client):
    """前往未知地点应 400（回归：此前 travel 端点漏了 scene_id 定义会 500）。"""
    c, ids = client
    sid = _make_session(c, ids)
    r = c.post(f"/api/sessions/{sid}/travel", json={"scene_id": "no_such_scene"})
    assert r.status_code == 400, r.text
    assert "尚未知晓" in r.json()["detail"]


def test_check_endpoint_logs_intent_alongside_skill(client, monkeypatch):
    """申请检定要带上『想对什么检定』的描述并落成可见行动记录——否则场景里同时有多条线索/
    多个可疑点时，KP 光看技能名猜不出玩家的具体目标。"""
    import app.api.chat as chat_module

    captured = {}

    def fake_start(session_id, coro, prelude=None):
        captured["session_id"] = session_id
        coro.close()  # 不真正触发生成（不碰 LLM），只验证 HTTP 层落地的行为

    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    resp = c.post(
        f"/api/sessions/{sid}/check",
        json={"skill": "侦查", "intent": "搜查书桌暗格"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["session_id"] == sid

    events = c.get(f"/api/sessions/{sid}/events").json()["events"]
    actions = [e["content"] for e in events if e["event_type"] == "action"]
    assert any("侦查" in a and "搜查书桌暗格" in a for a in actions)


def test_search_history_matches_and_returns_seq(client):
    """历史检索：模糊匹配本局的叙事/对话/行动等，返回 sequence_num 供前端跳转；排除系统噪音。"""
    from app.services import session_service

    c, ids = client
    sid = _make_session(c, ids)
    # 直接落几条事件（不触发生成）
    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    session_service.add_event(db, sid, "narration", "护士长站在走廊尽头，神色紧张。", actor_name="KP")
    session_service.add_event(db, sid, "dialogue", "地下室的钥匙在我这里。", actor_name="管家")
    session_service.add_event(db, sid, "system", "系统提示：地下室相关", actor_name="系统")
    gen.close()

    r = c.get(f"/api/sessions/{sid}/search", params={"q": "地下室"})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    contents = [x["content"] for x in results]
    assert any("钥匙" in x for x in contents)          # 命中对话
    assert all("系统提示" not in x for x in contents)   # 排除 system 噪音
    assert all("sequence_num" in x for x in results)   # 带定位信息

    # 空查询 → 空结果
    assert c.get(f"/api/sessions/{sid}/search", params={"q": ""}).json()["results"] == []


def test_chat_stashes_and_single_human_advance_triggers(client, monkeypatch):
    """回合确认制：/chat 只暂存（pending_turn）不触发；唯一真人 /advance 确认即整批交 KP，
    并把暂存发言转正。"""
    import app.api.chat as chat_module

    started = {"n": 0}

    def fake_start(session_id, coro, prelude=None):
        started["n"] += 1
        coro.close()

    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    assert c.post(f"/api/sessions/{sid}/chat", json={"content": "我推开门"}).status_code == 200
    assert started["n"] == 0  # 发言不触发生成
    evs = c.get(f"/api/sessions/{sid}/events").json()["events"]
    assert any(e["event_type"] == "action" and (e.get("metadata_") or {}).get("pending_turn") for e in evs)

    r = c.post(f"/api/sessions/{sid}/advance", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ready"] is True and started["n"] == 1  # 唯一真人确认 → 触发
    evs2 = c.get(f"/api/sessions/{sid}/events").json()["events"]
    assert not any((e.get("metadata_") or {}).get("pending_turn") for e in evs2)  # 已转正


def test_advance_waits_for_all_humans(client, monkeypatch):
    """多真人：需所有真人都确认后才触发；先确认的一方不 ready、不触发。"""
    import app.api.chat as chat_module

    started = {"n": 0}

    def fake_start(session_id, coro, prelude=None):
        started["n"] += 1
        coro.close()

    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    sid = c.post("/api/sessions", json={
        "module_id": ids["module"],
        "participants": [
            {"character_id": ids["hero"], "role": "human", "is_primary": True},
            {"character_id": ids["ally"], "role": "human"},
        ],
    }).json()["id"]

    r1 = c.post(f"/api/sessions/{sid}/advance", json={"acting_character_id": ids["hero"]})
    assert r1.json()["ready"] is False and started["n"] == 0  # 还差 ally
    r2 = c.post(f"/api/sessions/{sid}/advance", json={"acting_character_id": ids["ally"]})
    assert r2.json()["ready"] is True and started["n"] == 1  # 全确认 → 触发


def test_edit_and_delete_pending_events(client, monkeypatch):
    """玩家可改/删自己本回合尚未推进的暂存发言；改删都会重置本人确认。"""
    import app.api.chat as chat_module

    def _fake_start(session_id, coro, prelude=None):
        coro.close()
    monkeypatch.setattr(chat_module.generation_manager, "start", _fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    c.post(f"/api/sessions/{sid}/chat", json={"content": "我推开门"})
    c.post(f"/api/sessions/{sid}/chat", json={"content": "我环顾四周"})
    evs = c.get(f"/api/sessions/{sid}/events").json()["events"]
    pend = [e for e in evs if (e.get("metadata_") or {}).get("pending_turn")]
    assert len(pend) == 2
    eid1, eid2 = pend[0]["id"], pend[1]["id"]

    # 改写第一条
    r = c.patch(f"/api/sessions/{sid}/events/{eid1}", json={"content": "我轻轻推开那扇门"})
    assert r.status_code == 200, r.text
    # 删除第二条
    r2 = c.request("DELETE", f"/api/sessions/{sid}/events/{eid2}")
    assert r2.status_code == 200, r2.text

    evs2 = c.get(f"/api/sessions/{sid}/events").json()["events"]
    contents = {e["id"]: e["content"] for e in evs2}
    assert contents.get(eid1) == "我轻轻推开那扇门"     # 改写生效
    assert eid2 not in contents                          # 删除生效


def test_cannot_edit_non_pending_or_others_event(client, monkeypatch):
    """已推进（转正）的发言、或非本人的发言，不能改删。"""
    import app.api.chat as chat_module

    def _fake_start(session_id, coro, prelude=None):
        coro.close()
    monkeypatch.setattr(chat_module.generation_manager, "start", _fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    c.post(f"/api/sessions/{sid}/chat", json={"content": "我推开门"})
    eid = [e for e in c.get(f"/api/sessions/{sid}/events").json()["events"]
           if (e.get("metadata_") or {}).get("pending_turn")][0]["id"]
    # 推进 → 该发言转正（不再 pending）
    c.post(f"/api/sessions/{sid}/advance", json={})
    # 已转正 → 不能再改
    r = c.patch(f"/api/sessions/{sid}/events/{eid}", json={"content": "偷改"})
    assert r.status_code == 403


def test_regenerate_endpoint_cancels_and_restarts(client, monkeypatch):
    """重新生成：打断卡住的旧生成（cancel）→ 回滚 → 重启一次生成（start）。"""
    import app.api.chat as chat_module

    calls = {"cancel": 0, "start": 0}

    async def fake_cancel(session_id):
        calls["cancel"] += 1

    def fake_start(session_id, coro, prelude=None):
        calls["start"] += 1
        coro.close()

    monkeypatch.setattr(chat_module.generation_manager, "cancel", fake_cancel)
    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    resp = c.post(f"/api/sessions/{sid}/regenerate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls["cancel"] == 1 and calls["start"] == 1


def test_delete_session_requires_host_token(client):
    """有主会话只能房主删：无 token / 他人 token 一律 403，房主 token 才放行。

    回归：此前 delete 端点零校验，同网段任何人都能删掉整场存档。
    """
    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post(
        "/api/sessions",
        json={"module_id": ids["module"],
              "participants": [{"character_id": ids["hero"], "is_primary": True}]},
        headers=host,
    ).json()["id"]

    assert c.request("DELETE", f"/api/sessions/{sid}").status_code == 403  # 无 token
    assert c.request(
        "DELETE", f"/api/sessions/{sid}", headers={"X-Player-Token": "guest"}
    ).status_code == 403  # 他人 token
    assert c.request("DELETE", f"/api/sessions/{sid}", headers=host).status_code == 200


def test_list_sessions_hides_others_private_sessions(client):
    """列表按 token 过滤：房主的私有会话不出现在客人的『我的房间』里。"""
    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post(
        "/api/sessions",
        json={"module_id": ids["module"],
              "participants": [{"character_id": ids["hero"], "is_primary": True}]},
        headers=host,
    ).json()["id"]

    host_list = c.get("/api/sessions", headers=host).json()
    assert sid in {s["id"] for s in host_list}  # 房主自己可见
    guest_list = c.get("/api/sessions", headers={"X-Player-Token": "guest"}).json()
    assert sid not in {s["id"] for s in guest_list}  # 客人不可见


def test_resolve_actor_rejects_missing_token_on_owned_seat(client):
    """席位有归属时，缺 token 或 token 不匹配都不能以该角色行动（不再『token 为空即放行』）。"""
    from app.services import session_service

    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post(
        "/api/sessions",
        json={"module_id": ids["module"],
              "participants": [{"character_id": ids["hero"], "is_primary": True}]},
        headers=host,
    ).json()["id"]

    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        with pytest.raises(ValueError, match="无权"):
            session_service.resolve_actor(db, sid, None, ids["hero"])  # 缺 token
        with pytest.raises(ValueError, match="无权"):
            session_service.resolve_actor(db, sid, "guest", ids["hero"])  # 他人 token
        # 房主 token 放行
        char = session_service.resolve_actor(db, sid, "host-tok", ids["hero"])
        assert char.id == ids["hero"]
    finally:
        gen.close()


def test_end_module_single_player_one_click_and_reaches_growth(client):
    """单人：结束必须走投票端点；直接 PUT ended 已封死（400）；一票即结束，成长入口随之出现。"""
    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post(
        "/api/sessions",
        json={"module_id": ids["module"],
              "participants": [{"character_id": ids["hero"], "is_primary": True}]},
        headers=host,
    ).json()["id"]

    # 直接置 ended 的老路已封死：结束只能走全体共识投票
    assert c.put(
        f"/api/sessions/{sid}/status", json={"status": "ended"}, headers=host,
    ).status_code == 400
    # 非法状态仍 400
    assert c.put(
        f"/api/sessions/{sid}/status", json={"status": "bogus"}, headers=host,
    ).status_code == 400

    # 单人（仅房主）→ 一票即结束
    r = c.post(f"/api/sessions/{sid}/end-vote",
               json={"acting_character_id": ids["hero"]}, headers=host)
    assert r.status_code == 200, r.text
    assert r.json()["ended"] is True
    assert c.get(f"/api/sessions/{sid}").json()["status"] == "ended"
    # 已结束再投 → 400
    assert c.post(f"/api/sessions/{sid}/end-vote",
                  json={"acting_character_id": ids["hero"]}, headers=host).status_code == 400
    # 成长入口据 ended 出现
    assert c.get(f"/api/sessions/{sid}/growth",
                 params={"character_id": ids["hero"]}).status_code == 200


def test_end_module_multi_human_needs_all_agree(client):
    """多真人：先一人同意只推进投票不结束；撤销可清空；全部同意才真正结束。"""
    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post("/api/sessions", json={
        "module_id": ids["module"],
        "participants": [
            {"character_id": ids["hero"], "role": "human", "is_primary": True},
            {"character_id": ids["ally"], "role": "human"},
        ],
    }, headers=host).json()["id"]

    # 房主(hero)先同意 → 投票开启但未结束（还差 ally）
    r1 = c.post(f"/api/sessions/{sid}/end-vote",
                json={"acting_character_id": ids["hero"]}, headers=host)
    assert r1.status_code == 200 and r1.json()["ended"] is False
    v = r1.json()["vote"]
    assert v["agreed_count"] == 1 and v["total"] == 2 and v["open"] is True
    assert c.get(f"/api/sessions/{sid}").json()["status"] != "ended"

    # 撤销 → 清空（ally 席未认领，可无 token 撤）
    rc = c.request("DELETE", f"/api/sessions/{sid}/end-vote",
                   json={"acting_character_id": ids["ally"]})
    assert rc.status_code == 200 and rc.json()["vote"]["agreed_count"] == 0

    # 再来：hero 同意，ally 同意 → 全体一致 → 结束
    c.post(f"/api/sessions/{sid}/end-vote",
           json={"acting_character_id": ids["hero"]}, headers=host)
    r2 = c.post(f"/api/sessions/{sid}/end-vote", json={"acting_character_id": ids["ally"]})
    assert r2.status_code == 200 and r2.json()["ended"] is True
    assert c.get(f"/api/sessions/{sid}").json()["status"] == "ended"


def test_end_vote_rejects_impersonating_owned_seat(client):
    """冒用他人已认领角色投票被拒 403（token 不匹配席位归属）。"""
    c, ids = client
    host = {"X-Player-Token": "host-tok"}
    sid = c.post("/api/sessions",
                 json={"module_id": ids["module"],
                       "participants": [{"character_id": ids["hero"], "is_primary": True}]},
                 headers=host).json()["id"]
    # hero 已被 host 认领；guest 冒用 hero 投票 → 403
    r = c.post(f"/api/sessions/{sid}/end-vote", json={"acting_character_id": ids["hero"]},
               headers={"X-Player-Token": "guest"})
    assert r.status_code == 403


def test_offline_human_is_exempted_from_turn_confirm(client):
    """掉线的有主真人自动豁免：只要在线真人都确认即 ready，不被离线者卡死。"""
    from app.services import session_service

    c, ids = client
    host = {"X-Player-Token": "A"}
    sid = c.post("/api/sessions", json={
        "module_id": ids["module"],
        "participants": [
            {"character_id": ids["hero"], "role": "human", "is_primary": True},
            {"character_id": ids["ally"], "role": "human"},
        ],
    }, headers=host).json()["id"]

    gen = app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        # 把 ally 席位标记为归属玩家 B（模拟 B 曾入座后掉线）
        seat = (
            db.query(SessionParticipant)
            .filter(SessionParticipant.session_id == sid,
                    SessionParticipant.character_id == ids["ally"])
            .first()
        )
        seat.owner_token = "B"
        db.commit()

        # 只有 A 在线：需确认者应只剩 hero（ally 属离线的 B → 豁免）
        st = session_service.turn_confirm_state(db, sid, online_tokens={"A"})
        assert st["total"] == 1
        session_service.set_turn_confirm(db, sid, ids["hero"], True)
        st2 = session_service.turn_confirm_state(db, sid, online_tokens={"A"})
        assert st2["ready"] is True  # A 确认即可推进，不被离线的 B 卡死

        # 不传 online（旧行为）：两名真人都需确认，仍未 ready
        st3 = session_service.turn_confirm_state(db, sid)
        assert st3["total"] == 2 and st3["ready"] is False
    finally:
        gen.close()


def test_force_advance_host_only(client, monkeypatch):
    """房主强制推进：跳过未确认者直接触发；非房主被拒。"""
    import app.api.chat as chat_module

    started = {"n": 0}

    def fake_start(session_id, coro, prelude=None):
        started["n"] += 1
        coro.close()

    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    host = {"X-Player-Token": "A"}
    sid = c.post("/api/sessions", json={
        "module_id": ids["module"],
        "participants": [
            {"character_id": ids["hero"], "role": "human", "is_primary": True},
            {"character_id": ids["ally"], "role": "human"},
        ],
    }, headers=host).json()["id"]

    # 非房主不能强推
    assert c.post(f"/api/sessions/{sid}/force-advance",
                  headers={"X-Player-Token": "guest"}).status_code == 403
    assert started["n"] == 0
    # 房主强推 → 直接触发（不等 ally 确认）
    r = c.post(f"/api/sessions/{sid}/force-advance", headers=host)
    assert r.status_code == 200 and r.json()["ready"] is True
    assert started["n"] == 1


def test_check_endpoint_intent_optional(client, monkeypatch):
    """不填 intent 仍应正常申请检定（向后兼容旧客户端）。"""
    import app.api.chat as chat_module

    def fake_start(session_id, coro, prelude=None):
        coro.close()

    monkeypatch.setattr(chat_module.generation_manager, "start", fake_start)

    c, ids = client
    sid = _make_session(c, ids)
    resp = c.post(f"/api/sessions/{sid}/check", json={"skill": "侦查"})
    assert resp.status_code == 200, resp.text


def _client_with_scenes(tmp_path, scenes=None, visited=None):
    """自带带场景的模组 + 已访问两处的会话 + 无归属真人主席位——用于大地图前往。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import SessionParticipant

    engine = create_engine(
        f"sqlite:///{tmp_path / 'travel.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine)

    def override_get_db():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    db = TS()
    scenes = scenes or [{"id": "a", "title": "门厅"}, {"id": "b", "title": "图书馆"}]
    module = Module(title="M", rule_system="coc", npcs=[], scenes=scenes)
    hero = Character(name="主角", rule_system="coc", is_player=True)
    db.add_all([module, hero]); db.flush()
    session = GameSession(
        module_id=module.id, player_character_id=hero.id, status="active",
        current_scene_id="a", world_state={"visited_scenes": visited or ["a", "b"]},
    )
    db.add(session); db.flush()
    db.add(SessionParticipant(
        session_id=session.id, character_id=hero.id, role="human",
        is_primary=True, owner_token=None, claimed=True, ready=True,
    ))
    db.commit()
    sid, hid = session.id, hero.id
    db.close()
    return TestClient(app), sid, hid


def test_travel_stash_adds_pending_turn_and_commits_location(tmp_path, monkeypatch):
    """暂存式前往：stash=True 只加一条 pending_turn 行动、不触发生成；推进本回合后
    该行动转正并把角色位置确定性同步到目标场景（无需先说一句再手动点图）。"""
    import app.api.chat as chat_module
    from app.services import session_service

    c, sid, hid = _client_with_scenes(tmp_path)
    try:
        started = {"n": 0}
        monkeypatch.setattr(
            chat_module.generation_manager, "start",
            lambda session_id, coro, prelude=None: (started.__setitem__("n", started["n"] + 1), coro.close()),
        )

        r = c.post(f"/api/sessions/{sid}/travel", json={"scene_id": "b", "stash": True})
        assert r.status_code == 200, r.text
        assert r.json().get("stashed") is True
        assert started["n"] == 0  # 暂存不触发生成

        evs = c.get(f"/api/sessions/{sid}/events").json()["events"]
        pend = [e for e in evs if (e.get("metadata_") or {}).get("travel")]
        assert pend and pend[0]["metadata_"].get("pending_turn") is True
        assert pend[0]["metadata_"].get("scene_id") == "b"

        # 推进：暂存动作转正（pending_turn 清除，travel 元数据保留）
        assert c.post(f"/api/sessions/{sid}/advance", json={}).json()["ready"] is True
        evs2 = c.get(f"/api/sessions/{sid}/events").json()["events"]
        trav = [e for e in evs2 if (e.get("metadata_") or {}).get("travel")]
        assert trav and not trav[0]["metadata_"].get("pending_turn")  # 已转正

        # run_chat_generation 建 KP 上下文前调用的位置同步逻辑（生成本体被 monkeypatch 吞掉，
        # 故直接验证这段确定性副作用）：commit_pending_travel 把角色搬到目标场景。
        from app.services import chat_service

        sess = next(app.dependency_overrides[get_db]())
        try:
            chat_service.commit_pending_travel(sess, sid)
            assert session_service.get_char_location(sess.get(GameSession, sid), hid) == "b"
        finally:
            sess.close()
    finally:
        app.dependency_overrides.clear()


def test_travel_connectivity_gate(tmp_path, monkeypatch):
    """大地图前往的连通校验：不连通的已知地点 → 400 并提示可直达邻居；
    不相邻但连通（多跳）→ 放行（KP 会叙述途经）；模组没建图时行为不变（另有既有测试覆盖）。"""
    import app.api.chat as chat_module

    scenes = [
        {"id": "a", "title": "六号车厢", "connections": ["b"]},
        {"id": "b", "title": "四号车厢", "connections": ["c"]},
        {"id": "c", "title": "二号车厢"},
        {"id": "d", "title": "站台", "connections": ["e"]},   # 与列车不连通的孤岛
        {"id": "e", "title": "候车室", "connections": ["d"]},
    ]
    c, sid, _hid = _client_with_scenes(tmp_path, scenes=scenes, visited=["a", "c", "d"])
    try:
        started = {"n": 0}
        monkeypatch.setattr(
            chat_module.generation_manager, "start",
            lambda session_id, coro, prelude=None: (started.__setitem__("n", started["n"] + 1), coro.close()),
        )

        # 已知但不连通（六号车厢 → 站台）→ 400，且提示由此可直达的已知邻居
        r = c.post(f"/api/sessions/{sid}/travel", json={"scene_id": "d"})
        assert r.status_code == 400
        assert "不连通" in r.json()["detail"]
        assert started["n"] == 0

        # 不相邻但沿车厢连通（六号 → 二号，途经四号）→ 放行
        r2 = c.post(f"/api/sessions/{sid}/travel", json={"scene_id": "c"})
        assert r2.status_code == 200, r2.text
        assert started["n"] == 1
    finally:
        app.dependency_overrides.clear()
