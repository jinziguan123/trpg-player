from __future__ import annotations

import asyncio
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
SAN_CHECK_RE = re.compile(
    r"\[SAN_CHECK:\s*success_loss=([^,\]]+),?\s*failure_loss=([^\]]+)\]"
)
HP_CHANGE_RE = re.compile(
    r"\[HP_CHANGE:\s*target=([^,\]]+),?\s*delta=([^,\]]+),?\s*reason=([^\]]*)\]"
)
NPC_ACT_RE = re.compile(
    r"\[NPC_ACT:\s*npc_id=([^,\]]+),?\s*trigger=([^\]]+)\]"
)
SCENE_CHANGE_RE = re.compile(
    r"\[SCENE_CHANGE:\s*scene_id=([^\]]+)\]"
)

CMD_TAG_PREFIXES = ("DICE_CHECK:", "SAN_CHECK:", "HP_CHANGE:", "NPC_ACT:", "SCENE_CHANGE:")


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


async def _stream_narration_filtered(
    kp: KPAgent, messages: list[dict], result: list,
    npcs: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Stream KP narration, intercepting command tags and NPC dialogue.

    Yields ``narration`` chunks for descriptive text and ``npc_dialogue``
    chunks for quoted NPC speech detected inline (Chinese double-quotes
    “…”).  Command tags terminate the stream early.

    *result* is ``[narration_text, full_response, extracted_dialogues]``,
    mutated in place.
    """
    full_response = ""
    narration = ""
    pending = ""
    in_bracket = False
    bracket_buf = ""
    tag_found = False

    in_quote = False
    quote_buf = ""
    # Build (canonical_name, [searchable_parts]) for partial matching.
    # E.g. "托马斯·金博尔" → ["托马斯·金博尔", "托马斯", "金博尔"]
    npc_matchers: list[tuple[str, list[str]]] = []
    for _n in (npcs or []):
        _name = _n.get("name", "")
        if not _name:
            continue
        _parts = [_name]
        for _sep in ("·", "·", " ", "-"):
            if _sep in _name:
                _parts.extend(
                    p.strip() for p in _name.split(_sep) if len(p.strip()) >= 2
                )
                break
        npc_matchers.append((_name, _parts))
    extracted = result[2]
    last_speaker: str | None = None
    bracket_speaker: str | None = None
    bracket_dialogue_buf = ""

    def _match_npc(text: str) -> str | None:
        text = text.strip()
        for canonical, parts in npc_matchers:
            if text == canonical or text in parts:
                return canonical
        return None

    def _strip_npc_prefix(text: str) -> tuple[str, str | None]:
        s = text.rstrip()
        if not s:
            return text, None
        for canonical, parts in npc_matchers:
            for part in parts:
                for _sfx in (part + "：", part + "说道：", part + "说：", part + "说道，", part + "说，"):
                    if s.endswith(_sfx):
                        return s[:-len(_sfx)], canonical
        return text, None

    def _flush_bracket_dialogue():
        nonlocal bracket_speaker, bracket_dialogue_buf, last_speaker
        dialogue_text = bracket_dialogue_buf.strip()
        result_chunk = None
        if dialogue_text and bracket_speaker:
            last_speaker = bracket_speaker
            extracted.append((bracket_speaker, dialogue_text))
            result_chunk = _make_chunk(
                "npc_dialogue", dialogue_text,
                actor_name=bracket_speaker,
            )
        bracket_speaker = None
        bracket_dialogue_buf = ""
        return result_chunk

    async for token in kp.narrate(messages):
        full_response += token

        for ch in token:
            if in_bracket:
                bracket_buf += ch
                if ch == "]":
                    inner = bracket_buf[:-1]
                    if any(
                        inner.strip().startswith(p) for p in CMD_TAG_PREFIXES
                    ):
                        tag_found = True
                        break
                    matched_npc = _match_npc(inner) if not in_quote else None
                    if matched_npc:
                        if pending:
                            narration += pending
                            result[0] = narration
                            if pending.strip():
                                yield _make_chunk("narration", pending, actor_name="KP")
                            pending = ""
                        bracket_speaker = matched_npc
                        bracket_dialogue_buf = ""
                    else:
                        restored = "[" + bracket_buf
                        if in_quote:
                            quote_buf += restored
                        else:
                            pending += restored
                    bracket_buf = ""
                    in_bracket = False
            elif ch == "[":
                if bracket_speaker:
                    chunk = _flush_bracket_dialogue()
                    if chunk:
                        yield chunk
                in_bracket = True
                bracket_buf = ""
            elif ch == "“" and not in_quote:
                if bracket_speaker:
                    last_speaker = bracket_speaker
                    bracket_speaker = None
                    bracket_dialogue_buf = ""
                pending, _speaker = _strip_npc_prefix(pending)
                if _speaker:
                    last_speaker = _speaker
                if pending:
                    narration += pending
                    result[0] = narration
                    if pending.strip():
                        yield _make_chunk("narration", pending, actor_name="KP")
                    pending = ""
                in_quote = True
                quote_buf = ""
            elif ch == "”" and in_quote:
                in_quote = False
                dialogue_text = quote_buf.strip()
                attributed = False

                if len(dialogue_text) >= 2 and npc_matchers:
                    context = narration[-300:]
                    best_canonical: str | None = None
                    best_pos = -1
                    best_len = -1
                    for canonical, parts in npc_matchers:
                        for part in parts:
                            pos = context.rfind(part)
                            if pos >= 0 and (len(part), pos) > (best_len, best_pos):
                                best_pos = pos
                                best_len = len(part)
                                best_canonical = canonical
                    if best_canonical is None:
                        best_canonical = last_speaker
                    if best_canonical:
                        last_speaker = best_canonical
                        extracted.append((best_canonical, dialogue_text))
                        yield _make_chunk(
                            "npc_dialogue", dialogue_text,
                            actor_name=best_canonical,
                        )
                        attributed = True

                if not attributed:
                    pending += "“" + quote_buf + "”"
                quote_buf = ""
            else:
                if in_quote:
                    quote_buf += ch
                elif bracket_speaker:
                    bracket_dialogue_buf += ch
                    if bracket_dialogue_buf.endswith("\n\n"):
                        chunk = _flush_bracket_dialogue()
                        if chunk:
                            yield chunk
                else:
                    pending += ch

        if tag_found:
            if bracket_speaker:
                chunk = _flush_bracket_dialogue()
                if chunk:
                    yield chunk
            if pending:
                narration += pending
                result[0] = narration
                if pending.strip():
                    yield _make_chunk("narration", pending, actor_name="KP")
            break

        if not in_bracket and not in_quote and not bracket_speaker and pending:
            # Paragraph buffering: yield at \n\n boundaries
            while "\n\n" in pending:
                idx = pending.index("\n\n") + 2
                chunk = pending[:idx]
                _cs = chunk.rstrip()
                _hold = False
                if _cs and npc_matchers:
                    for _, _pts in npc_matchers:
                        for _p in _pts:
                            if any(_cs.endswith(_p + s) for s in ("：", "说道：", "说：", "说道，", "说，")):
                                _hold = True
                                break
                        if _hold:
                            break
                if _hold:
                    break
                pending = pending[idx:]
                narration += chunk
                result[0] = narration
                if chunk.strip():
                    yield _make_chunk("narration", chunk, actor_name="KP")
            # Sentence fallback for long buffers
            if len(pending) > 150:
                last_b = -1
                for _i, _ch in enumerate(pending):
                    if _ch in "\n。！？":
                        last_b = _i
                if last_b >= 0:
                    chunk = pending[: last_b + 1]
                    pending = pending[last_b + 1 :]
                    narration += chunk
                    result[0] = narration
                    if chunk.strip():
                        yield _make_chunk("narration", chunk, actor_name="KP")

    if not tag_found:
        if in_bracket:
            if in_quote:
                quote_buf += "[" + bracket_buf
            else:
                pending += "[" + bracket_buf
        if in_quote:
            pending += "“" + quote_buf
        if bracket_speaker:
            chunk = _flush_bracket_dialogue()
            if chunk:
                yield chunk
        if pending:
            narration += pending
            result[0] = narration
            if pending.strip():
                yield _make_chunk("narration", pending, actor_name="KP")

    result[0] = narration
    result[1] = full_response
    if len(result) > 2:
        result[2] = extracted


async def _run_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
) -> None:
    from app.services.generation_manager import generation_manager

    llm = get_llm()
    kp = KPAgent(llm)
    messages = build_kp_context(game_session, module, player_char, events)

    result = ["", "", []]
    async for chunk in _stream_narration_filtered(
        kp, messages, result, npcs=module.npcs,
    ):
        generation_manager.publish(session_id, chunk)

    narration_text = result[0].rstrip()
    if narration_text:
        session_service.add_event(
            db, session_id, "narration", narration_text, actor_name="KP",
        )
    for npc_name, dialogue_text in result[2]:
        session_service.add_event(
            db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
        )

    async for chunk in _process_commands(
        db, session_id, result[1], module, player_char, game_session, llm,
    ):
        generation_manager.publish(session_id, chunk)

    generation_manager.publish(session_id, _make_chunk("done"))


async def run_chat_generation(session_id: str) -> None:
    from app.database import SessionLocal
    from app.services.generation_manager import generation_manager

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        events = session_service.get_session_events(db, session_id)
        await _run_generation(db, session_id, game_session, module, player_char, events)
    except asyncio.CancelledError:
        logger.info("生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("生成失败: session=%s", session_id)
        generation_manager.publish(session_id, _make_chunk("system", "生成出错，请重试"))
    finally:
        db.close()


async def run_opening_generation(session_id: str) -> None:
    from app.database import SessionLocal
    from app.services.generation_manager import generation_manager

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        await _run_generation(db, session_id, game_session, module, player_char, [])
    except asyncio.CancelledError:
        logger.info("开场生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("开场生成失败: session=%s", session_id)
        generation_manager.publish(session_id, _make_chunk("system", "生成出错，请重试"))
    finally:
        db.close()


def _update_character_stat(db: Session, char: Character, path: str, value) -> None:
    """更新角色 system_data 中的嵌套字段并持久化"""
    sd = dict(char.system_data or {})
    parts = path.split(".")
    target = sd
    for p in parts[:-1]:
        if p not in target or not isinstance(target[p], dict):
            target[p] = {}
        target[p] = dict(target[p])
        target = target[p]
    target[parts[-1]] = value
    char.system_data = sd
    db.add(char)
    db.commit()
    db.refresh(char)


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

    for match in SAN_CHECK_RE.finditer(kp_text):
        success_loss = match.group(1).strip()
        failure_loss = match.group(2).strip()

        from app.rules.coc.checks import san_check
        char_data = {
            "base_attributes": player_char.base_attributes,
            "skills": player_char.skills,
            "system_data": player_char.system_data,
        }
        result = san_check(char_data, success_loss, failure_loss)
        check = result["check"]

        _update_character_stat(db, player_char, "sanity.current", result["new_san"])

        outcome_text = "成功" if check.outcome in ("critical_success", "hard_success", "success") else "失败"
        dice_content = (
            f"🎲 理智检定：{check.description}\n"
            f"SAN 损失：{result['san_loss']}（{result['old_san']} → {result['new_san']}）"
        )
        if result["went_insane"]:
            dice_content += "\n⚠️ 短暂疯狂！（一次性损失 SAN ≥ 当前 SAN/5）"

        dice_meta = {
            "skill": "SAN",
            "skill_value": result["old_san"],
            "roll": check.roll,
            "target": check.target,
            "outcome": outcome_text,
            "san_loss": result["san_loss"],
            "new_san": result["new_san"],
            "went_insane": result["went_insane"],
        }
        session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        yield _make_chunk("dice", dice_content, metadata=dice_meta)
        dice_descriptions.append(
            f"理智检定（{outcome_text}）：损失 {result['san_loss']} SAN（{result['old_san']}→{result['new_san']}）"
        )

    for match in HP_CHANGE_RE.finditer(kp_text):
        target_str = match.group(1).strip()
        delta_str = match.group(2).strip()
        reason = match.group(3).strip()

        if target_str == "player":
            try:
                delta = int(delta_str)
            except ValueError:
                continue
            hp_data = player_char.system_data.get("hitPoints", {})
            old_hp = hp_data.get("current", 0)
            max_hp = hp_data.get("max", old_hp)
            new_hp = max(0, min(max_hp, old_hp + delta))

            _update_character_stat(db, player_char, "hitPoints.current", new_hp)

            if delta < 0:
                hp_content = f"💔 {player_char.name} 受到 {abs(delta)} 点伤害（HP {old_hp} → {new_hp}）"
                if reason:
                    hp_content += f"——{reason}"
                if abs(delta) >= max_hp // 2:
                    hp_content += "\n⚠️ 重伤！"
                if new_hp <= 0:
                    hp_content += "\n☠️ 濒死！"
            else:
                hp_content = f"💚 {player_char.name} 恢复 {delta} 点生命（HP {old_hp} → {new_hp}）"
                if reason:
                    hp_content += f"——{reason}"

            session_service.add_event(
                db, session_id, "system", hp_content,
                actor_name="系统", metadata={"hp_change": delta, "old_hp": old_hp, "new_hp": new_hp},
            )
            yield _make_chunk("system", hp_content)

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
        cont_result = ["", "", []]
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, cont_result, npcs=module.npcs,
            ):
                yield chunk
        finally:
            cont_narration = cont_result[0].rstrip()
            if cont_narration:
                session_service.add_event(
                    db, session_id, "narration", cont_narration, actor_name="KP",
                )
            for npc_name, dialogue_text in cont_result[2]:
                session_service.add_event(
                    db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
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
