from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai.agents.kp_agent import KPAgent
from app.ai.agents.npc_agent import NPCAgent
from app.ai.agents.team_agent import TeamAgent
from app.ai.context import build_kp_context, build_npc_context, build_team_context
from app.ai.llm_factory import get_llm
from app.ai.prompts.kp_system import (
    CHECK_REQUEST_PROMPT,
    KP_DICE_CONTINUATION_PROMPT,
    KP_RULE_CONTINUATION_PROMPT,
)
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import map_service, rulebook_service, session_service
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)

# DICE_CHECK 升级为键值解析（参数顺序无关）：skill=必填；difficulty/char/visibility 选填。
# char=对谁投（空/主角=主角，队友名，NPC 名）；visibility=open|blind（blind=暗投/暗骰，结果只给 KP）。
DICE_CHECK_RE = re.compile(r"\[DICE_CHECK:([^\]]*)\]")
# 对抗骰：两方各投同名或不同技能，比成功等级。a/b 为角色名（主角/队友/NPC）。
OPPOSED_CHECK_RE = re.compile(r"\[OPPOSED_CHECK:([^\]]*)\]")
# SAN_CHECK 升级为键值解析：success_loss/failure_loss + chars=（目睹者，缺省在场全体）
# + source=（恐怖源标识，用于「同一角色对同一恐怖只检定一次」的去重）。各角色各自结算。
SAN_CHECK_RE = re.compile(r"\[SAN_CHECK:([^\]]*)\]")
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
# 剧情状态推进：KP 在叙事节拍发 [SET_FLAG: flag=xxx] 置标志、[CLEAR_FLAG: flag=xxx] 清标志，
# 场景/NPC 的状态变体据此切换（如「地下室进水后变致命」「危险消退」）。是内部控制标签，不展示给玩家。
SET_FLAG_RE = re.compile(r"\[SET_FLAG:\s*flag=([^\]]+)\]")
CLEAR_FLAG_RE = re.compile(r"\[CLEAR_FLAG:\s*flag=([^\]]+)\]")
# 走位：KP 在叙述里就地标记角色移动到某锚点（物体/出口/NPC/其他角色/坐标）。内联剔除、不打断叙述。
MOVE_RE = re.compile(r"\[MOVE:([^\]]*)\]")
# 分头行动：KP 在每个分组/场景内容前标 [GROUP: scene=<场景标签>]，后续内容归该组，前端据此分栏。内联剔除。
GROUP_RE = re.compile(r"\[GROUP:([^\]]*)\]")

# 无名/泛称 NPC 的说话人前缀识别（如「护工：」「一位医生说道：」），用于把其台词正确
# 归到该身份名下，而不是硬塞给某个有名有姓的 NPC。
# A：带说话动词（动词消歧，较安全，可内联）；B：裸「X：」仅当独立成标签（前为句末/冒号/换行/引号）。
_SPEAKER_VERB_RE = re.compile(
    r"([一-龥·]{2,5})(?:说道|说|问道|问|答道|答|开口道|低声道|喊道|叫道|笑道|沉声道|轻声道)[：:，,]\s*$"
)
_SPEAKER_LABEL_RE = re.compile(
    r"(?:^|[。！？!?\n：:」』])\s*([一-龥·]{2,5})[：:]\s*$"
)
# 书写/铭刻/标牌类语境：其后引号是「书写/标识内容」而非台词，应留在旁白、绝不抽成对话气泡。
# 允许标识名词与引号之间夹分隔符（——、、：、空格 等），覆盖「贴着褪色的字牌——「…」」这类。
_WRITTEN_TEXT_RE = re.compile(
    r"(写着|写道|写有|刻着|刻有|记着|记载|标着|印着|贴着|挂着|题写|题着|题为|落款|显示|显现|上书|"
    r"字牌|牌子|招牌|门牌|标牌|标签|标题|铭牌|告示|名为|名叫|写作)"
    r"[：:，,、\s—\-]*$"
)
# 易误判为说话人的旁白连接词/状语（裸「X：」分支用），出现则不当作说话人。
_NON_SPEAKER = {
    "这时", "此时", "随后", "接着", "然后", "突然", "忽然", "紧接", "与此", "另一",
    "其中", "只见", "声音", "众人", "大家", "对方", "他们", "她们", "它们", "远处", "身后",
}

# 「在说话」的动词线索：角色名紧随其后才认定为说话人，区别于「仅被提及」。
_SPEAK_CUES = (
    "说", "道", "问", "答", "开口", "低声", "嘀咕", "喊", "叫", "吼", "沉吟",
    "补充", "继续", "回答", "应", "讲", "嘟囔", "轻声", "冷笑", "叹", "笑",
)

CMD_TAG_PREFIXES = (
    "DICE_CHECK:", "OPPOSED_CHECK:", "SAN_CHECK:", "HP_CHANGE:", "NPC_ACT:",
    "SCENE_CHANGE:", "RULE_LOOKUP:", "SET_FLAG:", "CLEAR_FLAG:",
)

# 单次生成内最多连续查阅规则书的次数（防止 KP 反复查导致长链/慢）
MAX_RULE_LOOKUPS = 2
# 骰子续写里 KP 可能再发指令（如读懂禁忌知识后追加 SAN_CHECK）→ 允许继续处理，
# 但限制连锁深度防止无限掷骰链（检定→续写→再检定→…）。
MAX_DICE_CONTINUATIONS = 3

# 中/英文小括号内为 OOC（场外交流）：只在房间内广播，不进入 KP 上下文、不触发生成。
OOC_RE = re.compile(r"（[^（）]*）|\([^()]*\)")

# 引号内为「说出口的台词」（speak），引号外为「行动」（act）：用于把玩家的言/行分流。
# 支持中文双引号 “”、中文方引号 「」『』、ASCII 双引号 "。
QUOTE_RE = re.compile(r'[“"「『]([^”"」』]*)[”"」』]')


def split_speech_action(text: str) -> list[tuple[str, str]]:
    """按引号约定把一段玩家输入拆成有序的（类型, 文本）片段。

    引号内 → ``dialogue``（说出口的台词）；引号外的非空文字 → ``action``（行动）。
    完全不含引号时整条按 ``action``。保留原文顺序，便于 KP 与渲染按序呈现。
    """
    segments: list[tuple[str, str]] = []
    last = 0
    for m in QUOTE_RE.finditer(text or ""):
        before = (text[last : m.start()] or "").strip(" \t\n，,。.、")
        if before:
            segments.append(("action", before))
        inner = (m.group(1) or "").strip()
        if inner:
            segments.append(("dialogue", inner))
        last = m.end()
    tail = (text[last:] if text else "").strip(" \t\n，,。.、")
    if tail:
        segments.append(("action", tail))
    return segments


# 模组未给某 NPC 某技能数值时的兜底基线（普通人水平），保证 NPC 检定可解析。
DEFAULT_NPC_SKILL = 45

# 要求难度的中文标签（用于「请进行一个【困难】的【侦查】检定」提示；normal 不带难度词）。
DIFFICULTY_LABEL = {"normal": "", "hard": "困难", "extreme": "极难"}

# 达成等级中文标签（六档），与引擎 CheckResult.tier 对应。
TIER_LABEL = {
    "critical": "大成功", "extreme": "极难成功", "hard": "困难成功",
    "regular": "普通成功", "fail": "普通失败", "fumble": "大失败",
}


def _check_prompt_text(actor_name: str, skill: str, difficulty: str) -> str:
    """req 1：系统主动给出的检定提示语。"""
    diff = DIFFICULTY_LABEL.get(difficulty, "")
    if diff:
        return f"请 {actor_name} 进行一次「{diff}」难度的「{skill}」检定"
    return f"请 {actor_name} 进行一次「{skill}」检定"

# 成功等级排序（对抗骰比较用）：大成功 > 困难/极难成功 > 普通成功 > 失败 > 大失败。
_OUTCOME_RANK = {
    "critical_success": 4,
    "hard_success": 3,
    "success": 2,
    "failure": 1,
    "fumble": 0,
}


def _parse_tag_kv(inner: str) -> dict[str, str]:
    """把 ``a=x, b=y`` 形式的指令参数解析成 dict（顺序无关，容空格）。"""
    out: dict[str, str] = {}
    for part in (inner or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _resolve_check_actor(
    char_ref: str,
    skill_name: str,
    player_char: Character,
    teammates: list[Character] | None,
    module: Module,
) -> tuple[dict, str, bool, str | None]:
    """把 char= 解析成 (character_data, 显示名, is_npc, char_id)。

    空/主角→主角；队友名→对应队友；NPC 名→用模组 NPC 数值卡（缺该技能用 DEFAULT_NPC_SKILL
    兜底）。匹配不到时兜底当作主角，避免检定无法进行。char_id 为对应玩家角色的 id（NPC 为 None）。
    """
    name = (char_ref or "").strip()

    def cdata_of(c: Character) -> dict:
        return {
            "base_attributes": c.base_attributes,
            "skills": c.skills,
            "system_data": c.system_data,
        }

    if not name or name in ("主角", "玩家", player_char.name):
        return cdata_of(player_char), player_char.name, False, player_char.id
    for t in (teammates or []):
        if t.name and (t.name == name or name in t.name or t.name in name):
            return cdata_of(t), t.name, False, t.id
    for npc in (module.npcs or []):
        nm = npc.get("name", "")
        if nm and (nm == name or name in nm or nm in name):
            skills = dict(npc.get("skills") or {})
            if skill_name and skill_name not in skills:
                skills[skill_name] = DEFAULT_NPC_SKILL
            return {"base_attributes": {}, "skills": skills, "system_data": {}}, nm, True, None
    return cdata_of(player_char), player_char.name, False, player_char.id


_ALL_TOKENS = {"在场", "全体", "全部", "所有人", "所有", "all", "everyone"}


def _resolve_san_targets(
    chars_ref: str | None,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """把 SAN_CHECK 的 chars= 解析成目睹者角色列表（玩家方角色一视同仁，无主角特权）。

    空或「在场/全体/all」→ 全队；否则按名单（逗号/顿号分隔）匹配，匹配不到兜底全队。
    """
    party = [player_char] + list(teammates or [])
    ref = (chars_ref or "").strip()
    if not ref or ref.lower() in _ALL_TOKENS or ref in _ALL_TOKENS:
        return party
    names = [n.strip() for n in re.split(r"[,，、/]", ref) if n.strip()]
    out: list[Character] = []
    for n in names:
        for c in party:
            if c.name and (c.name == n or n in c.name or c.name in n) and c not in out:
                out.append(c)
    return out or party


async def _resolve_opposed(
    db, session_id, kv, engine, module, player_char, teammates, dice_descriptions,
):
    """对抗骰：两方各投一次，比成功等级；同级比技能值高者胜，再平则平局。

    参数：a/b（或 actor/target）= 角色名；a_skill/b_skill（缺省取 skill）= 各自技能。
    """
    a_ref = (kv.get("a") or kv.get("actor") or "").strip()
    b_ref = (kv.get("b") or kv.get("target") or "").strip()
    a_skill = (kv.get("a_skill") or kv.get("skill") or "").strip()
    b_skill = (kv.get("b_skill") or a_skill).strip()
    if not a_skill or not b_skill:
        return

    a_data, a_name, _, _ = _resolve_check_actor(a_ref, a_skill, player_char, teammates, module)
    b_data, b_name, _, _ = _resolve_check_actor(b_ref, b_skill, player_char, teammates, module)
    a_res = engine.resolve_check(a_data, a_skill, "normal")
    b_res = engine.resolve_check(b_data, b_skill, "normal")

    ar, br = _OUTCOME_RANK.get(a_res.outcome, 1), _OUTCOME_RANK.get(b_res.outcome, 1)
    if ar != br:
        winner = a_name if ar > br else b_name
    elif a_res.skill_value != b_res.skill_value:
        winner = a_name if a_res.skill_value > b_res.skill_value else b_name
    else:
        winner = "平局"

    verdict = f"{winner} 胜" if winner != "平局" else "平局"
    dice_content = (
        f"对抗骰　{a_name}（{a_skill}）{a_res.description}　vs　"
        f"{b_name}（{b_skill}）{b_res.description}　→　{verdict}"
    )
    dice_meta = {
        "opposed": True,
        "a": {"actor": a_name, "skill": a_skill, "roll": a_res.roll,
              "target": a_res.target, "outcome": a_res.outcome},
        "b": {"actor": b_name, "skill": b_skill, "roll": b_res.roll,
              "target": b_res.target, "outcome": b_res.outcome},
        "winner": winner,
    }
    ev = session_service.add_event(
        db, session_id, "dice", dice_content, actor_name="系统", metadata=dice_meta,
    )
    yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id)
    dice_descriptions.append(
        f"对抗骰：{a_name}({a_skill}) {a_res.description} vs "
        f"{b_name}({b_skill}) {b_res.description} → {verdict}"
    )


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
    quote_written = False  # 当前引号是否为「书写内容」（写着：/刻着：…）→ 留旁白不抽对话
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
    # 记录每段对话插入时的旁白偏移 (offset_in_narration, name, text)，持久化时据此
    # 把整段旁白按偏移切开、与对话交错还原——使「生成结束后从 DB 对齐」的渲染顺序与
    # 流式时一致（旁白/对话交错），而非旁白全在前、对话全在后。
    dialogue_marks: list = result[3] if len(result) > 3 else []

    def _mark_dlg(name: str, text: str) -> None:
        dialogue_marks.append((len(narration), name, text))

    # 分头行动分组标记：(旁白偏移, 组标签)，持久化时据此给各段/对话打 group
    group_marks: list = result[4] if len(result) > 4 else []

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
        # 1) 已知 NPC 名前缀优先（归一到 canonical）
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                for _sfx in (part + "：", part + "说道：", part + "说：", part + "说道，", part + "说，"):
                    if s.endswith(_sfx):
                        return s[:-len(_sfx)], canonical
        # 书写/铭刻语境（写着：/刻着：…）不是说话人，交由 quote_written 当书写内容留旁白
        if _WRITTEN_TEXT_RE.search(s):
            return text, None
        # 2) 泛化：无名/泛称说话人（护工/医生/老板/老妇人…）——带说话动词或独立成标签的「X：」
        for _rx in (_SPEAKER_VERB_RE, _SPEAKER_LABEL_RE):
            m = _rx.search(s)
            if m:
                name = m.group(1)
                if name not in _NON_SPEAKER:
                    # 命中已知 NPC 的局部名（如「史蒂芬」）则归一到全名；否则保留泛称（护工…）
                    return s[:m.start(1)], (_match_npc(name) or name)
        return text, None

    def _flush_bracket_dialogue():
        nonlocal bracket_speaker, bracket_dialogue_buf, last_speaker
        dialogue_text = bracket_dialogue_buf.strip()
        result_chunk = None
        if dialogue_text and bracket_speaker:
            last_speaker = bracket_speaker
            extracted.append((bracket_speaker, dialogue_text))
            _mark_dlg(bracket_speaker, dialogue_text)
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
                    if inner.strip().startswith("MOVE:"):
                        # 内联走位标记：从旁白剔除、不终止流（仍留在 full_response 供 _process_commands 解析）
                        bracket_buf = ""
                        in_bracket = False
                        continue
                    if inner.strip().startswith("GROUP:"):
                        # 内联分组标记：记录从此处起后续内容所属的分组/场景，从旁白剔除、不终止流
                        kv = _parse_tag_kv(inner.strip()[len("GROUP:"):])
                        label = (kv.get("scene") or kv.get("group") or kv.get("name") or "").strip()
                        group_marks.append((len(narration), label or None))
                        bracket_buf = ""
                        in_bracket = False
                        continue
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
                # 书写/铭刻语境（写着：/刻着：…）→ 本引号是书写内容，留旁白不抽成对话
                quote_written = bool(_WRITTEN_TEXT_RE.search(pending.rstrip()))
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

                if quote_written:
                    quote_written = False  # 书写内容：不抽对话，下方 not attributed 分支留回旁白
                elif len(dialogue_text) >= 2 and npc_matchers:
                    recent = narration[-160:]
                    best_canonical: str | None = None
                    # 1) 显式说话线索：角色名紧随说话动词/冒号（如「史蒂芬说道：」「沃尔特低声」）。
                    #    只有这种「在说话」的信号才切换说话人；「仅被提及」的名字（如剧情里
                    #    谈到的历史人物）不算，避免把当前说话人的台词错挂到被提及者头上。
                    cue_speaker: str | None = None
                    cue_is_player = False
                    cue_pos = -1
                    for canonical, parts, is_player in npc_matchers:
                        for part in parts:
                            start = 0
                            while True:
                                p = recent.find(part, start)
                                if p < 0:
                                    break
                                after = recent[p + len(part): p + len(part) + 8]
                                if after[:1] in ("：", ":") or any(v in after for v in _SPEAK_CUES):
                                    if p > cue_pos:
                                        cue_pos, cue_speaker, cue_is_player = p, canonical, is_player
                                start = p + 1
                    if cue_speaker is not None:
                        # 玩家方角色被点名说话 → KP 误代言/书写内容，不抽取（留旁白）
                        best_canonical = None if cue_is_player else cue_speaker
                    elif last_speaker is not None:
                        # 无新说话人线索 → 沿用当前说话人，别被仅被提及的名字夺走
                        best_canonical = last_speaker
                    else:
                        # 首句且无线索：只认**紧贴引号前**（最近 24 字）出现的 NPC——即该句正在
                        # 描述的当事人。远处仅被提及的名字（如另一段里的前租户）不作说话人，
                        # 避免把门牌/招牌等「带引号的标签文本」错挂成某 NPC 的台词。
                        near = narration[-24:]
                        best_pos = -1
                        best_len = -1
                        best_is_player = False
                        for canonical, parts, is_player in npc_matchers:
                            for part in parts:
                                pos = near.rfind(part)
                                if pos >= 0 and (len(part), pos) > (best_len, best_pos):
                                    best_pos, best_len = pos, len(part)
                                    best_canonical, best_is_player = canonical, is_player
                        if best_is_player:
                            best_canonical = None
                    if best_canonical:
                        last_speaker = best_canonical
                        extracted.append((best_canonical, dialogue_text))
                        _mark_dlg(best_canonical, dialogue_text)
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
    if len(result) > 3:
        result[3] = dialogue_marks
    if len(result) > 4:
        result[4] = group_marks


MAX_TEAMMATES_PER_TURN = 4

# check：队友主动发起的技能检定（content 描述尝试、skill 给技能），先落 action 事件再掷骰。
TEAM_ACTION_EVENT = {
    "speak": "dialogue", "act": "action", "assist": "action", "check": "action",
}


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
    return {
        "action": action,
        "content": str(data.get("content") or "").strip(),
        "skill": str(data.get("skill") or "").strip(),  # 仅 action=check 时有意义
    }


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
        # speak 用 npc_dialogue 走前端气泡渲染；act/assist/check 走通用 action 渲染
        chunk_type = "npc_dialogue" if event_type == "dialogue" else "action"
        yield _make_chunk(
            chunk_type, content, actor_name=teammate.name,
            event_id=ev.id, actor_id=teammate.id,
        )
        # 队友主动检定：紧接着掷骰（明骰），结果落库交由 KP 收束叙述
        if action == "check" and decision.get("skill"):
            skill = decision["skill"]
            engine = get_engine(module.rule_system)
            cdata = {
                "base_attributes": teammate.base_attributes,
                "skills": teammate.skills,
                "system_data": teammate.system_data,
            }
            result = engine.resolve_check(cdata, skill, "normal")
            dice_content = f"{teammate.name}｜{skill} 检定（normal）：{result.description}"
            dice_meta = {
                "skill": skill, "skill_value": result.skill_value,
                "roll": result.roll, "target": result.target,
                "outcome": result.outcome, "actor": teammate.name,
            }
            dev = session_service.add_event(
                db, session_id, "dice", dice_content,
                actor_name="系统", metadata=dice_meta,
            )
            yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=dev.id)


def _persist_error_notice(db: Session, session_id: str, text: str) -> None:
    """把生成中断提示落库为 system 事件，保证在客户端 resync 后仍可见。"""
    try:
        session_service.add_event(db, session_id, "system", text, actor_name="系统")
    except Exception:
        logger.exception("落库生成中断提示失败: session=%s", session_id)


def _persist_narration(db: Session, session_id: str, result: list) -> None:
    """落库 KP 这一轮产物，保留旁白与对话的交错顺序（与流式渲染一致）。

    用 result[3] 里记录的「对话插入偏移」把整段旁白切开、与对话交错落库；
    没有偏移信息（旧调用）时回退为「旁白整段在前、对话在后」。
    """
    narration = result[0]
    marks = result[3] if len(result) > 3 else None
    group_marks = sorted(result[4], key=lambda g: g[0]) if len(result) > 4 and result[4] else []

    def _group_at(offset: int) -> str | None:
        g = None
        for off, label in group_marks:
            if off <= offset:
                g = label
            else:
                break
        return g

    def _add_narr(text: str, offset: int) -> None:
        t = text.rstrip()
        if t:
            session_service.add_event(
                db, session_id, "narration", t, actor_name="KP", group=_group_at(offset),
            )

    if marks is not None:
        pos = 0
        for off, npc_name, dialogue_text in sorted(marks, key=lambda m: m[0]):
            off = max(pos, min(off, len(narration)))
            _add_narr(narration[pos:off], pos)
            if dialogue_text:
                session_service.add_event(
                    db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
                    group=_group_at(off),
                )
            pos = off
        _add_narr(narration[pos:], pos)
        return

    # 回退：无交错信息
    _add_narr(narration, 0)
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

    result = ["", "", [], [], []]
    # 取消（硬取消 task）或流式中途报错（如供应商抖动断流）时，已生成的叙事都要落库，
    # 否则客户端在收到 done 后 resync 会拉到空历史，造成「生成到一半聊天全部消失」。
    matcher_npcs = _matcher_npcs(module, teammates)
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, result, npcs=matcher_npcs,
        ):
            room_hub.broadcast(session_id, chunk)
    except BaseException:
        # CancelledError(继承 BaseException) 与普通异常都先把已生成片段落库再上抛
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
        _persist_error_notice(db, session_id, "（KP 生成中断，请重试或继续输入）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def _run_kp_turn(
    db, session_id, game_session, module, player_char, party_others, user_prompt: str,
) -> None:
    """跑一轮 KP：注入 user_prompt → 流式叙事 → 处理指令（待定检定/掷骰/场景等）→ done。"""
    llm = get_llm()
    events = session_service.get_session_events(db, session_id)
    rules_enabled = rulebook_service.has_rulebook(db, module.rule_system)
    messages = build_kp_context(
        game_session, module, player_char, events,
        teammates=party_others, rules_lookup_enabled=rules_enabled,
    )
    messages.append({"role": "user", "content": user_prompt})

    kp = KPAgent(llm)
    res = ["", "", [], [], []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, res, npcs=_matcher_npcs(module, party_others),
        ):
            room_hub.broadcast(session_id, chunk)
    except asyncio.CancelledError:
        _persist_narration(db, session_id, res)
        raise
    _persist_narration(db, session_id, res)

    async for chunk in _process_commands(
        db, session_id, res[1], module, player_char, game_session, llm,
        teammates=party_others,
    ):
        room_hub.broadcast(session_id, chunk)
    room_hub.broadcast(session_id, _make_chunk("done"))


async def run_check_request_generation(
    session_id: str, actor_id: str, skill: str,
) -> None:
    """玩家『申请』检定：交 KP 裁定本次是否需要检定、用什么难度（玩家不指定难度）。

    KP 若判定需要，会输出 [DICE_CHECK]，经 _process_commands 挂成「待玩家投骰」；
    若判定无需检定，则直接简短叙述。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        actor = db.get(Character, actor_id) or player_char
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            CHECK_REQUEST_PROMPT.format(actor=actor.name, skill=skill),
        )
    except asyncio.CancelledError:
        logger.info("检定申请生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("检定申请生成失败: session=%s", session_id)
        room_hub.broadcast(session_id, _make_chunk("system", "生成出错，请重试"))
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_roll_generation(session_id: str, check_id: str) -> None:
    """玩家点『投骰』：取出待定检定 → 按 KP 定的难度掷骰 → 广播达成等级 → KP 据等级续写。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        check = session_service.pop_pending_check(db, session_id, check_id)
        if not check:
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )

        skill = check["skill"]
        difficulty = check.get("difficulty", "normal")
        source = check.get("source", "")
        char_data, disp_name, _is_npc, _cid = _resolve_check_actor(
            check.get("char_ref", ""), skill, player_char, party_others, module,
        )
        engine = get_engine(module.rule_system)
        result = engine.resolve_check(char_data, skill, difficulty)
        tier_cn = TIER_LABEL.get(result.tier, result.tier)

        dice_content = (
            f"{disp_name}｜{skill} 检定（{difficulty}）：{tier_cn}（{result.description}）"
        )
        dice_meta = {
            "skill": skill, "skill_value": result.skill_value, "roll": result.roll,
            "target": result.target, "outcome": result.outcome, "tier": result.tier,
            "actor": disp_name,
        }
        ev = session_service.add_event(
            db, session_id, "dice", dice_content, actor_name="系统", metadata=dice_meta,
        )
        room_hub.broadcast(
            session_id,
            _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id),
        )

        desc = (
            f"{disp_name} {skill}（{difficulty}），达成 {tier_cn}"
            + (f"（针对：{source}）" if source else "")
            + f"：{result.description}"
        )
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            KP_DICE_CONTINUATION_PROMPT.format(dice_results=desc),
        )
    except asyncio.CancelledError:
        logger.info("投骰生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("投骰生成失败: session=%s", session_id)
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
        # 落库系统提示（而非仅广播）：否则客户端收到 done 后 resync 会把它一并抹掉
        _persist_error_notice(db, session_id, "（开场生成中断，请点重试或刷新）")
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
    dice_depth: int = 0,
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

    from app.rules.coc.checks import san_check

    # 同一角色对同一恐怖源只检定一次：用 world_state.san_checked 记 "source|char_id"。
    ws = dict(game_session.world_state or {})
    san_checked = set(ws.get("san_checked") or [])
    san_dirty = False

    for match in SAN_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        success_loss = (kv.get("success_loss") or "0").strip()
        failure_loss = (kv.get("failure_loss") or "1d6").strip()
        source = (kv.get("source") or "").strip()
        targets = _resolve_san_targets(kv.get("chars"), player_char, teammates)

        for tchar in targets:
            key = f"{source}|{tchar.id}" if source else None
            if key and key in san_checked:
                continue  # 该角色已对此恐怖源检定过，不重复

            char_data = {
                "base_attributes": tchar.base_attributes,
                "skills": tchar.skills,
                "system_data": tchar.system_data,
            }
            result = san_check(char_data, success_loss, failure_loss)
            check = result["check"]
            _update_character_stat(db, tchar, "sanity.current", result["new_san"])

            outcome_text = "成功" if check.outcome in (
                "critical_success", "hard_success", "success") else "失败"
            dice_content = (
                f"{tchar.name}｜理智检定：{check.description}\n"
                f"SAN 损失：{result['san_loss']}（{result['old_san']} → {result['new_san']}）"
            )
            if result["went_insane"]:
                dice_content += "\n短暂疯狂！（一次性损失 SAN ≥ 当前 SAN/5）"

            dice_meta = {
                "skill": "SAN",
                "actor": tchar.name,
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
                f"{tchar.name} 理智检定（{outcome_text}）：损失 {result['san_loss']} SAN"
                f"（{result['old_san']}→{result['new_san']}）"
            )
            if key:
                san_checked.add(key)
                san_dirty = True

    if san_dirty:
        ws["san_checked"] = sorted(san_checked)
        game_session.world_state = ws
        db.add(game_session)
        db.commit()

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
                hp_content = f"{player_char.name} 受到 {abs(delta)} 点伤害（HP {old_hp} → {new_hp}）"
                if reason:
                    hp_content += f"——{reason}"
                if abs(delta) >= max_hp // 2:
                    hp_content += "\n重伤！"
                if new_hp <= 0:
                    hp_content += "\n濒死！"
            else:
                hp_content = f"{player_char.name} 恢复 {delta} 点生命（HP {old_hp} → {new_hp}）"
                if reason:
                    hp_content += f"——{reason}"

            ev = session_service.add_event(
                db, session_id, "system", hp_content,
                actor_name="系统", metadata={"hp_change": delta, "old_hp": old_hp, "new_hp": new_hp},
            )
            yield _make_chunk("system", hp_content, event_id=ev.id)

    engine = get_engine(module.rule_system)

    for match in DICE_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        skill_name = (kv.get("skill") or "").strip()
        if not skill_name:
            continue
        difficulty = (kv.get("difficulty") or "normal").strip() or "normal"
        char_ref = (kv.get("char") or "").strip()
        blind = (kv.get("visibility") or "open").strip().lower() == "blind"
        source = (kv.get("source") or "").strip()

        char_data, disp_name, is_npc, char_id = _resolve_check_actor(
            char_ref, skill_name, player_char, teammates, module,
        )

        # req 1/2：真人控制、且非暗投的检定 → 不自动掷，挂成「待玩家投骰」并给出提示；
        # NPC 暗骰 / AI 队友 / 暗投 仍由系统自动掷（无人点投骰，避免卡住）。
        if (
            not is_npc and not blind
            and session_service.is_human_controlled(db, session_id, char_id)
        ):
            check_id = uuid.uuid4().hex
            pending = {
                "id": check_id, "skill": skill_name, "difficulty": difficulty,
                "char_ref": char_ref, "char_id": char_id, "actor_name": disp_name,
                "source": source,
            }
            session_service.add_pending_check(db, session_id, pending)
            prompt_text = _check_prompt_text(disp_name, skill_name, difficulty)
            meta = {"check_request": True, **pending}
            ev = session_service.add_event(
                db, session_id, "system", prompt_text, actor_name="系统", metadata=meta,
            )
            yield _make_chunk(
                "check_request", prompt_text, metadata=meta,
                event_id=ev.id, actor_id=char_id,
            )
            continue  # 等玩家 /roll，本轮不掷、不续写

        result = engine.resolve_check(char_data, skill_name, difficulty)
        tier_cn = TIER_LABEL.get(result.tier, result.tier)

        if blind:
            # 暗投（玩家/队友）/暗骰（NPC）：聊天只显示"做了一次隐藏检定"，成败仅回灌 KP。
            kind_word = "暗骰" if is_npc else "暗投"
            dice_content = f"{disp_name} 进行了一次{kind_word}·{skill_name}（结果仅 KP 可见）"
            dice_meta = {"skill": skill_name, "actor": disp_name, "blind": True}
            dice_descriptions.append(
                f"【{kind_word}·{disp_name}·{skill_name}（{difficulty}），结果仅你（KP）可见，"
                f"绝不可直接把成败告诉玩家】：达成 {tier_cn}；{result.description}"
            )
        else:
            dice_content = (
                f"{disp_name}｜{skill_name} 检定（{difficulty}）：{tier_cn}（{result.description}）"
            )
            dice_meta = {
                "skill": skill_name,
                "skill_value": result.skill_value,
                "roll": result.roll,
                "target": result.target,
                "outcome": result.outcome,
                "tier": result.tier,
                "actor": disp_name,
            }
            dice_descriptions.append(
                f"{disp_name} {skill_name}（{difficulty}），达成 {tier_cn}"
                + (f"（针对：{source}）" if source else "")
                + f"：{result.description}"
            )

        ev = session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id)

    for match in OPPOSED_CHECK_RE.finditer(kp_text):
        async for chunk in _resolve_opposed(
            db, session_id, _parse_tag_kv(match.group(1)),
            engine, module, player_char, teammates, dice_descriptions,
        ):
            yield chunk

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
        cont_result = ["", "", [], [], []]
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
        # 续写里 KP 可能再发指令（如读懂禁忌知识后追加 [SAN_CHECK]、或场景切换）——
        # 继续处理，但限深度防无限掷骰链。
        if dice_depth + 1 < MAX_DICE_CONTINUATIONS:
            async for chunk in _process_commands(
                db, session_id, cont_result[1], module, player_char, game_session, llm,
                teammates=teammates, allow_rule_lookup=False, dice_depth=dice_depth + 1,
            ):
                yield chunk

    for match in SCENE_CHANGE_RE.finditer(kp_text):
        new_scene_id = match.group(1).strip()
        session_service.update_scene(db, session_id, new_scene_id)
        yield _make_chunk("system", f"场景切换至：{new_scene_id}")

    # 剧情状态推进：置/清标志后，刷新内存里的 game_session.world_state，使本次生成的后续
    # 处理（续写、NPC 行动）与下一轮上下文都能看到最新状态。
    for match in SET_FLAG_RE.finditer(kp_text):
        flag = match.group(1).strip()
        session_service.set_flag(db, session_id, flag, True)
        db.refresh(game_session)
        yield _make_chunk("system", f"剧情推进：{flag}")
    for match in CLEAR_FLAG_RE.finditer(kp_text):
        flag = match.group(1).strip()
        session_service.set_flag(db, session_id, flag, False)
        db.refresh(game_session)
        yield _make_chunk("system", f"剧情状态解除：{flag}")

    # 走位：把 [MOVE: actor, to] 解析成场景内坐标并落库（地图随 refreshTick 重新拉取反映）
    for match in MOVE_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        actor = (kv.get("actor") or kv.get("char") or "").strip()
        target = (kv.get("to") or kv.get("target") or "").strip()
        if actor and target:
            try:
                map_service.apply_move(db, game_session, actor, target)
            except Exception:
                logger.exception("走位更新失败: actor=%s to=%s", actor, target)

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
    yield _make_chunk("system", "守秘人翻阅规则书……")

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
    cont_result = ["", "", [], [], []]
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
