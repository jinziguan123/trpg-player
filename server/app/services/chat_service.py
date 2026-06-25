from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai.agents.kp_agent import KPAgent
from app.ai.agents.npc_agent import NPCAgent
from app.ai.context import build_kp_context, build_npc_context
from app.ai.deepseek import get_llm
from app.ai.prompts.kp_system import KP_DICE_CONTINUATION_PROMPT
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import session_service

logger = logging.getLogger(__name__)

DICE_CHECK_RE = re.compile(
    r"\[DICE_CHECK:\s*skill=([^,\]]+),?\s*difficulty=(\w+)\]"
)
NPC_ACT_RE = re.compile(
    r"\[NPC_ACT:\s*npc_id=([^,\]]+),?\s*trigger=([^\]]+)\]"
)
SCENE_CHANGE_RE = re.compile(
    r"\[SCENE_CHANGE:\s*scene_id=([^\]]+)\]"
)


def _make_chunk(
    chunk_type: str,
    content: str = "",
    actor_name: str | None = None,
    metadata: dict | None = None,
) -> str:
    data = {"type": chunk_type, "content": content}
    if actor_name:
        data["actor_name"] = actor_name
    if metadata:
        data["metadata"] = metadata
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def handle_chat(
    db: Session, session_id: str, player_input: str
) -> AsyncIterator[str]:
    game_session = db.get(GameSession, session_id)
    if not game_session:
        yield _make_chunk("system", "会话不存在")
        yield _make_chunk("done")
        return

    if game_session.status != "active":
        yield _make_chunk("system", "会话未处于活跃状态")
        yield _make_chunk("done")
        return

    module = db.get(Module, game_session.module_id)
    player_char = db.get(Character, game_session.player_character_id)

    if not module or not player_char:
        yield _make_chunk("system", "模组或角色数据缺失")
        yield _make_chunk("done")
        return

    session_service.add_event(
        db, session_id, "dialogue", player_input,
        actor_id=player_char.id, actor_name="玩家",
    )

    events = session_service.get_session_events(db, session_id)

    llm = get_llm()
    kp = KPAgent(llm)
    messages = build_kp_context(game_session, module, player_char, events)

    full_response = ""
    async for token in kp.narrate(messages):
        full_response += token
        yield _make_chunk("narration", token, actor_name="KP")

    session_service.add_event(
        db, session_id, "narration", full_response, actor_name="KP",
    )

    async for chunk in _process_commands(
        db, session_id, full_response, module, player_char, game_session, llm
    ):
        yield chunk

    yield _make_chunk("done")


async def handle_opening(
    db: Session, session_id: str
) -> AsyncIterator[str]:
    game_session = db.get(GameSession, session_id)
    if not game_session:
        yield _make_chunk("system", "会话不存在")
        yield _make_chunk("done")
        return

    module = db.get(Module, game_session.module_id)
    player_char = db.get(Character, game_session.player_character_id)

    if not module or not player_char:
        yield _make_chunk("system", "模组或角色数据缺失")
        yield _make_chunk("done")
        return

    llm = get_llm()
    kp = KPAgent(llm)
    messages = build_kp_context(game_session, module, player_char, [])

    full_response = ""
    async for token in kp.narrate(messages):
        full_response += token
        yield _make_chunk("narration", token, actor_name="KP")

    session_service.add_event(
        db, session_id, "narration", full_response, actor_name="KP",
    )

    yield _make_chunk("done")


async def _process_commands(
    db: Session,
    session_id: str,
    kp_text: str,
    module: Module,
    player_char: Character,
    game_session: GameSession,
    llm,
) -> AsyncIterator[str]:
    dice_descriptions: list[str] = []

    for match in DICE_CHECK_RE.finditer(kp_text):
        skill_name = match.group(1).strip()
        difficulty = match.group(2).strip()

        engine = get_engine(module.rule_system)
        char_data = {
            "base_attributes": player_char.base_attributes,
            "skills": player_char.skills,
            "system_data": player_char.system_data,
        }
        result = engine.resolve_check(char_data, skill_name, difficulty)

        dice_content = (
            f"🎲 {skill_name} 检定（{difficulty}）：{result.description}"
        )
        dice_meta = {
            "skill": skill_name,
            "skill_value": result.skill_value,
            "roll": result.roll,
            "target": result.target,
            "outcome": result.outcome,
        }

        session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        yield _make_chunk("dice", dice_content, metadata=dice_meta)
        dice_descriptions.append(
            f"{skill_name}（{difficulty}）：{result.description}"
        )

    if dice_descriptions:
        continuation_prompt = KP_DICE_CONTINUATION_PROMPT.format(
            dice_results="\n".join(dice_descriptions)
        )
        events = session_service.get_session_events(db, session_id)
        messages = build_kp_context(game_session, module, player_char, events)
        messages.append({"role": "user", "content": continuation_prompt})

        kp = KPAgent(llm)
        continuation = ""
        async for token in kp.narrate(messages):
            continuation += token
            yield _make_chunk("narration", token, actor_name="KP")

        session_service.add_event(
            db, session_id, "narration", continuation, actor_name="KP",
        )

    for match in SCENE_CHANGE_RE.finditer(kp_text):
        new_scene_id = match.group(1).strip()
        session_service.update_scene(db, session_id, new_scene_id)
        yield _make_chunk("system", f"场景切换至：{new_scene_id}")

    for match in NPC_ACT_RE.finditer(kp_text):
        npc_id = match.group(1).strip()
        trigger = match.group(2).strip()

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

        session_service.add_event(
            db, session_id, "dialogue", npc_response,
            actor_id=npc_id, actor_name=npc_name,
            visibility=[npc_id, player_char.id],
        )
        yield _make_chunk("dialogue", npc_response, actor_name=npc_name)
