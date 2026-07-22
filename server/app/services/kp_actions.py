"""KP 的 NPC、追逐、战斗和结构化台词动作。"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.ai.agents.npc_agent import NPCAgent
from app.ai.context import build_npc_context
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import illustration_service, session_service, turn_context, world_memory
from app.services.event_protocol import make_chunk as _make_chunk

_attach_npc_portrait = illustration_service._attach_npc_portrait
_maybe_encounter_illustration = illustration_service._maybe_encounter_illustration
_apply_world_memory = turn_context._apply_world_memory
_scene_title = turn_context._scene_title


async def _exec_npc_act(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    llm, player_char: Character, npc_id: str, trigger: str,
) -> tuple[list[str], str]:
    """触发 NPC 人格代理行动/开口，台词落库并广播。返回 (chunks, NPC 台词)。"""
    events = session_service.get_session_events(db, session_id)
    npc_messages = build_npc_context(
        npc_id, game_session, module, events, trigger_context=trigger,
    )

    npc_def = None
    for n in (module.npcs or []):
        if n.get("id") == npc_id:
            npc_def = n
            break
    npc_name = npc_def["name"] if npc_def else npc_id

    npc_agent = NPCAgent(llm, npc_id)
    npc_response = await npc_agent.respond(npc_messages)

    ev = session_service.add_event(
        db, session_id, "dialogue", npc_response,
        actor_id=npc_id, actor_name=npc_name,
        visibility=[npc_id, player_char.id],
    )
    # 立绘钩子：缓存命中会直接写进事件 metadata → 随对话 chunk 一并带上（气泡即时出头像）
    _attach_npc_portrait(db, session_id, module, ev)
    portrait = (ev.metadata_ or {}).get("portrait")
    chunks = [_make_chunk(
        "dialogue", npc_response, actor_name=npc_name,
        event_id=ev.id, actor_id=npc_id,
        metadata={"portrait": portrait} if portrait else None,
    )]
    # 世界记忆钩子 b：NPC 被触发行动后记入其互动史（trigger 原文截断，不调 LLM）
    _apply_world_memory(
        db, game_session,
        lambda ws: world_memory.record_npc_interaction(
            ws, npc_id, ev.sequence_num, f"受场景触发而行动/开口：{trigger}",
        ),
    )
    return chunks, npc_response


def _exec_start_chase(
    db: Session, session_id: str, module: Module, player_char: Character,
    pursuer_str: str, trigger: str,
) -> list[str]:
    """start_chase 工具：玩家作逃方，pursuer 按名字解析模组 NPC（匹配不到按临场追兵建）。返回 chunks。"""
    from app.services import chase_service

    nm = (pursuer_str or "").strip() or "追兵"
    spec = next((n for n in (module.npcs or []) if n.get("name") == nm or n.get("id") == nm), None)
    pursuer = chase_service._pursuer_from_npc(
        spec or {"name": nm, "attributes": {"DEX": 50, "CON": 50, "SIZ": 50}, "skills": {"运动": 45}})
    quarry = chase_service._quarry_from_char(player_char)
    _state, chunks = chase_service.start_chase(db, session_id, quarry, pursuer, trigger=trigger)
    return chunks


async def _exec_start_combat(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    player_char: Character, teammates: list[Character] | None, llm,
    enemies_str: str, trigger: str,
) -> list[str]:
    """start_combat 工具：按敌方名字解析模组 NPC，把玩家方（主角+队友）与敌方切入战斗态，
    自动推进到第一个真人回合。返回广播 chunks。名字匹配不到的敌方按临场杂兵建（默认属性）。"""
    from app.ai.agents.combat_agent import CombatAgent
    from app.services import combat_service

    names = [n.strip() for n in re.split(r"[，,、]", enemies_str or "") if n.strip()]
    npc_by = {n.get("name"): n for n in (module.npcs or [])}
    npc_by_id = {n.get("id"): n for n in (module.npcs or [])}
    enemies: list[dict] = []
    for nm in names or ["敌人"]:
        spec = npc_by.get(nm) or npc_by_id.get(nm)
        enemies.append(dict(spec) if spec else {"name": nm, "attributes": {"DEX": 50, "CON": 50, "SIZ": 50},
                                                "skills": {"格斗(斗殴)": 45, "闪避": 25}, "weapon": "徒手格斗"})
    party = [player_char] + list(teammates or [])
    human_ids = session_service.human_character_ids(db, session_id) or {player_char.id}
    scene_hint = _scene_title(module, game_session.current_scene_id)
    # 遭遇配图卡先落先播（战斗态 chunks 紧随其后）：卡先出、图异步补挂，不阻塞开战
    illust_chunks = _maybe_encounter_illustration(db, session_id, module, enemies)
    agent = CombatAgent(llm) if llm is not None else None
    _state, chunks = await combat_service.start(
        db, session_id, party, enemies, human_ids, trigger,
        agent=agent, scene_hint=scene_hint,
    )
    return illust_chunks + chunks


def _exec_say(result: list, module: Module, who: str, text: str) -> list[str]:
    """say() 工具：把一句 NPC 台词作为对话气泡广播，并**记入 result 的对话交错标记**——
    落库交给收尾的 _persist_narration 按偏移与旁白交错持久化（复用旧路径的成熟机制），
    从而在 resync 时与旁白保持正确先后顺序（不能在 loop 中直接落库，那会先于旁白）。

    who 尽量归一到模组 NPC 的规范名；解析不到按临场龙套用原名。返回待广播的 chunks。
    此路径不经台词过滤器的启发式猜测——对话直接由模型的结构化调用给出。
    """
    npc_name = who
    for n in (module.npcs or []):
        if n.get("id") == who or n.get("name") == who:
            npc_name = n.get("name") or who
            break
    # 偏移＝此刻已累计旁白长度（本步旁白已并入 result[0]）→ 台词插在本步旁白之后、下步之前。
    offset = len(result[0])
    if len(result) > 3:
        result[3].append((offset, npc_name, text))
    if len(result) > 2:
        result[2].append((npc_name, text))  # 供 _record_npc_say_memory 记入 NPC 互动史
    return [_make_chunk("dialogue", text, actor_name=npc_name)]
