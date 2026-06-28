from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai.agents.kp_agent import KPAgent
from app.ai.agents.npc_agent import NPCAgent
from app.ai.agents.team_agent import TeamAgent
from app.ai.context import build_kp_context, build_npc_context, build_team_context
from app.ai.llm_factory import get_llm
from app.ai.prompts.kp_system import (
    KP_DICE_CONTINUATION_PROMPT,
    KP_RULE_CONTINUATION_PROMPT,
)
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import rulebook_service, session_service
from app.services.room_hub import room_hub

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
RULE_LOOKUP_RE = re.compile(
    r"\[RULE_LOOKUP:\s*query=([^\]]+)\]"
)

CMD_TAG_PREFIXES = (
    "DICE_CHECK:", "SAN_CHECK:", "HP_CHANGE:", "NPC_ACT:", "SCENE_CHANGE:",
    "RULE_LOOKUP:",
)

# 单次生成内最多连续查阅规则书的次数（防止 KP 反复查导致长链/慢）
MAX_RULE_LOOKUPS = 2

# 中/英文小括号内为 OOC（场外交流）：只在房间内广播，不进入 KP 上下文、不触发生成。
OOC_RE = re.compile(r"（[^（）]*）|\([^()]*\)")


def split_ooc(text: str) -> tuple[str, str]:
    """拆出正式行动与 OOC 内容。

    返回 ``(in_character, ooc)``：``in_character`` 是去掉括号段后的正式行动文本，
    ``ooc`` 是括号内文字（去掉括号、多段以空格连接）。
    """
    ooc_parts = OOC_RE.findall(text or "")
    in_character = OOC_RE.sub("", text or "").strip()
    ooc = " ".join(p[1:-1].strip() for p in ooc_parts if len(p) >= 2).strip()
    return in_character, ooc


def _make_chunk(
    chunk_type: str,
    content: str = "",
    actor_name: str | None = None,
    metadata: dict | None = None,
    event_id: str | None = None,
    actor_id: str | None = None,
) -> str:
    data: dict = {"type": chunk_type, "content": content}
    if actor_name:
        data["actor_name"] = actor_name
    if metadata:
        data["metadata"] = metadata
    # 持久离散事件携带 id/actor_id：供 /live 重连时按 id 去重、判定行动者归属
    if event_id:
        data["id"] = event_id
    if actor_id:
        data["actor_id"] = actor_id
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _matcher_npcs(module: Module, teammates: list[Character] | None) -> list[dict]:
    """供行内台词归属用的名字表：模组 NPC + 在场队友（真人/AI）。

    队友不在 module.npcs 里，若不加进来，KP 偶尔替队友写的引号台词会被
    错误归给附近提到的某个模组 NPC（如把约翰·卡特的话记到萨沙·卡纳头上）。
    """
    extra = [{"name": t.name, "is_player": True} for t in (teammates or []) if t.name]
    return (module.npcs or []) + extra


def event_to_chunk(ev) -> str:
    """把一条持久 EventLog 序列化为 /live 重放用的 chunk。"""
    type_map = {"dialogue": "dialogue", "action": "action", "dice": "dice",
                "narration": "narration_full", "system": "system", "ooc": "ooc"}
    return _make_chunk(
        type_map.get(ev.event_type, ev.event_type),
        ev.content,
        actor_name=ev.actor_name or None,
        metadata=ev.metadata_ or None,
        event_id=ev.id,
        actor_id=ev.actor_id,
    )


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
    # (canonical, [searchable_parts], is_player)。玩家方角色（真人/AI 队友）只用于
    # 命中后**阻止**抽取，绝不作为说话人——玩家不通过 KP 旁白发言，旁白里靠近其名字
    # 的引号文本多是书写/刻字内容或 KP 误代言，应留在旁白而非渲染成其对话气泡。
    npc_matchers: list[tuple[str, list[str], bool]] = []
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
        npc_matchers.append((_name, _parts, bool(_n.get("is_player"))))
    extracted = result[2]
    last_speaker: str | None = None
    bracket_speaker: str | None = None
    bracket_dialogue_buf = ""

    def _match_npc(text: str) -> str | None:
        text = text.strip()
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            if text == canonical or text in parts:
                return canonical
        return None

    def _strip_npc_prefix(text: str) -> tuple[str, str | None]:
        s = text.rstrip()
        if not s:
            return text, None
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
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
                    best_is_player = False
                    for canonical, parts, is_player in npc_matchers:
                        for part in parts:
                            pos = context.rfind(part)
                            if pos >= 0 and (len(part), pos) > (best_len, best_pos):
                                best_pos = pos
                                best_len = len(part)
                                best_canonical = canonical
                                best_is_player = is_player
                    if best_is_player:
                        # 最近的匹配是玩家方角色 → 该引号不是 NPC 台词（书写/刻字内容
                        # 或 KP 误代言）→ 不抽取，整段留在旁白里
                        best_canonical = None
                    elif best_canonical is None:
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
                    for _, _pts, _ in npc_matchers:
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


MAX_TEAMMATES_PER_TURN = 4

TEAM_ACTION_EVENT = {"speak": "dialogue", "act": "action", "assist": "action"}


def _parse_team_decision(raw) -> dict | None:
    """解析队友 agent 的 JSON 决策；失败返回 None（编排层据此 hold）。"""
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return None
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    else:
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in TEAM_ACTION_EVENT and action != "silent":
        return None
    return {"action": action, "content": str(data.get("content") or "").strip()}


async def _run_team_turn(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    teammates: list[Character],
    llm,
) -> AsyncIterator[str]:
    """玩家输入后的一轮 AI 队友自动响应。

    每个队友只决策一次；结果写入事件流，并依次让后续队友 / KP 看到。
    本函数只由 ``run_chat_generation`` 调用，不会自触发，故不存在递归链式生成。
    """
    for teammate in teammates[:MAX_TEAMMATES_PER_TURN]:
        events = session_service.get_session_events(db, session_id)
        messages = build_team_context(
            teammate, game_session, module, events, player_char,
            all_teammates=teammates,
        )
        agent = TeamAgent(llm, teammate.id)
        try:
            raw = await agent.decide(messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("队友决策失败: char=%s", teammate.id)
            continue
        decision = _parse_team_decision(raw)
        if not decision:
            continue  # 解析失败：hold，不重试不递归
        action = decision["action"]
        content = decision["content"]
        if action == "silent" or not content:
            continue
        event_type = TEAM_ACTION_EVENT[action]
        ev = session_service.add_event(
            db, session_id, event_type, content,
            actor_id=teammate.id, actor_name=teammate.name,
        )
        # speak 用 npc_dialogue 走前端气泡渲染；act/assist 走通用 action 渲染
        chunk_type = "npc_dialogue" if event_type == "dialogue" else "action"
        yield _make_chunk(
            chunk_type, content, actor_name=teammate.name,
            event_id=ev.id, actor_id=teammate.id,
        )


def _persist_narration(db: Session, session_id: str, result: list) -> None:
    narration_text = result[0].rstrip()
    if narration_text:
        session_service.add_event(
            db, session_id, "narration", narration_text, actor_name="KP",
        )
    for npc_name, dialogue_text in result[2]:
        session_service.add_event(
            db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
        )


async def _run_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None = None,
) -> None:
    llm = get_llm()
    kp = KPAgent(llm)
    # 仅在非开场、且该规则系统已挂载规则书时，向 KP 广告 [RULE_LOOKUP] 能力
    rules_enabled = bool(events) and rulebook_service.has_rulebook(db, module.rule_system)
    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=rules_enabled,
    )

    result = ["", "", []]
    # try/finally 保证流被取消（如硬取消生成 task）时已生成的叙事仍落库，
    # 避免「刷新丢失」类问题；成功路径只落库一次。
    matcher_npcs = _matcher_npcs(module, teammates)
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, result, npcs=matcher_npcs,
        ):
            room_hub.broadcast(session_id, chunk)
    except asyncio.CancelledError:
        _persist_narration(db, session_id, result)
        raise
    _persist_narration(db, session_id, result)

    async for chunk in _process_commands(
        db, session_id, result[1], module, player_char, game_session, llm,
        teammates=teammates,
    ):
        room_hub.broadcast(session_id, chunk)

    room_hub.broadcast(session_id, _make_chunk("done"))


async def run_chat_generation(session_id: str) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        ai_teammates = session_service.get_ai_teammates(db, session_id)
        # KP 上下文整队：主角之外的所有已填角色（真人 + AI），让 KP 知道全员在场
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )

        # 玩家输入后：先跑一轮 AI 队友自动响应（仅 AI 席、仅一轮、不自触发），再交 KP 收束
        if ai_teammates:
            llm = get_llm()
            async for chunk in _run_team_turn(
                db, session_id, game_session, module, player_char, ai_teammates, llm,
            ):
                room_hub.broadcast(session_id, chunk)

        events = session_service.get_session_events(db, session_id)
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others,
        )
    except asyncio.CancelledError:
        logger.info("生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "生成出错，请重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_opening_generation(session_id: str) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        # 幂等：已有事件（开局已生成）则不重复生成，只收尾，防止重复开场。
        existing = session_service.get_session_events(db, session_id, limit=1)
        if existing:
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        # 开场不跑队友回合（尚无玩家行动），但把队伍信息带进 KP 上下文让其知道谁在场
        await _run_generation(
            db, session_id, game_session, module, player_char, [],
            teammates=party_others,
        )
    except asyncio.CancelledError:
        logger.info("开场生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("开场生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "生成出错，请重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
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
    teammates: list[Character] | None = None,
    allow_rule_lookup: bool = True,
    lookup_depth: int = 0,
) -> AsyncIterator[str]:
    # 规则书查阅是终止性指令（独占一次回复的最后一行）：命中即查阅+续写，不再处理本段其余
    if allow_rule_lookup and lookup_depth < MAX_RULE_LOOKUPS:
        lookup = RULE_LOOKUP_RE.search(kp_text)
        if lookup:
            async for chunk in _handle_rule_lookup(
                db, session_id, lookup.group(1).strip(), module, player_char,
                game_session, llm, teammates=teammates, lookup_depth=lookup_depth,
            ):
                yield chunk
            return

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
        ev = session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id)
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

            ev = session_service.add_event(
                db, session_id, "system", hp_content,
                actor_name="系统", metadata={"hp_change": delta, "old_hp": old_hp, "new_hp": new_hp},
            )
            yield _make_chunk("system", hp_content, event_id=ev.id)

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

        ev = session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id)
        dice_descriptions.append(
            f"{skill_name}（{difficulty}）：{result.description}"
        )

    if dice_descriptions:
        continuation_prompt = KP_DICE_CONTINUATION_PROMPT.format(
            dice_results="\n".join(dice_descriptions)
        )
        events = session_service.get_session_events(db, session_id)
        messages = build_kp_context(
            game_session, module, player_char, events, teammates=teammates,
        )
        messages.append({"role": "user", "content": continuation_prompt})

        kp = KPAgent(llm)
        cont_result = ["", "", []]
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, cont_result, npcs=_matcher_npcs(module, teammates),
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

        ev = session_service.add_event(
            db, session_id, "dialogue", npc_response,
            actor_id=npc_id, actor_name=npc_name,
            visibility=[npc_id, player_char.id],
        )
        yield _make_chunk(
            "dialogue", npc_response, actor_name=npc_name,
            event_id=ev.id, actor_id=npc_id,
        )


async def _handle_rule_lookup(
    db: Session,
    session_id: str,
    query: str,
    module: Module,
    player_char: Character,
    game_session: GameSession,
    llm,
    teammates: list[Character] | None = None,
    lookup_depth: int = 0,
) -> AsyncIterator[str]:
    """KP 发起 [RULE_LOOKUP] 后：检索规则书原文 → 回灌让 KP 据此续写裁定。

    透明提示一条 ephemeral system（不落库）；检索不到时给降级文案让 KP 凭经验处理。
    续写产物再过一遍 _process_commands（禁再查阅），以便"查完规则随即发起检定"成立。
    """
    yield _make_chunk("system", "📖 守秘人翻阅规则书……")

    hits = rulebook_service.retrieve(db, query, module.rule_system, k=3)
    if hits:
        passages = "\n\n".join(f"[第 {h['page']} 页] {h['text']}" for h in hits)
    else:
        passages = "（未在规则书中找到直接匹配的内容，请依据《裁定手册》与你的经验处理。）"

    continuation = KP_RULE_CONTINUATION_PROMPT.format(query=query, passages=passages)
    events = session_service.get_session_events(db, session_id)
    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=False,  # 续写阶段不再广告查阅，避免长链
    )
    messages.append({"role": "user", "content": continuation})

    kp = KPAgent(llm)
    cont_result = ["", "", []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, cont_result, npcs=_matcher_npcs(module, teammates),
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

    # 续写里可能含查完规则后发起的检定/场景切换等，照常处理（但禁止再次查阅）
    async for chunk in _process_commands(
        db, session_id, cont_result[1], module, player_char, game_session, llm,
        teammates=teammates, allow_rule_lookup=False, lookup_depth=lookup_depth + 1,
    ):
        yield chunk
