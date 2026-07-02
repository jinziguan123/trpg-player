from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai import story_summarizer, turn_planner, turn_validator
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
    r"\[SCENE_CHANGE:\s*(?:scene_id=)?([^\]]+)\]"  # 容忍漏写 scene_id= 的形式
)
RULE_LOOKUP_RE = re.compile(
    r"\[RULE_LOOKUP:\s*query=([^\]]+)\]"
)
# 剧情状态推进：KP 在叙事节拍发 [SET_FLAG: flag=xxx] 置标志、[CLEAR_FLAG: flag=xxx] 清标志，
# 场景/NPC 的状态变体据此切换（如「地下室进水后变致命」「危险消退」）。是内部控制标签，不展示给玩家。
# 容忍：漏写「flag=」、冒号写成空格（如「[SET_FLAG hint_x]」）。全角括号在处理前已归一为半角。
SET_FLAG_RE = re.compile(r"\[SET_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
CLEAR_FLAG_RE = re.compile(r"\[CLEAR_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
# 走位：KP 在叙述里就地标记角色移动到某锚点（物体/出口/NPC/其他角色/坐标）。内联剔除、不打断叙述。
MOVE_RE = re.compile(r"\[MOVE:([^\]]*)\]")
# 分头行动：KP 在每个分组/场景内容前标 [GROUP: scene=<场景标签>]，后续内容归该组，前端据此分栏。内联剔除。
GROUP_RE = re.compile(r"\[GROUP:([^\]]*)\]")

# 书写/标识语境：其后引号是书写/标识内容（非台词），留旁白。允许标识名词与引号间夹分隔符。
_WRITTEN_TEXT_RE = re.compile(
    r"(写着|写道|写有|刻着|刻有|记着|记载|标着|印着|贴着|挂着|题写|题着|题为|落款|显示|显现|上书|"
    r"字牌|牌子|招牌|门牌|标牌|标签|标题|铭牌|告示|名为|名叫|写作|条目|卡片|抽出一张|一行字|"
    r"短讯|电讯|报道|头条|标语|新闻|登载|刊载|载有)"
    # 线索/书写内容常带 markdown 标记或换行（如「写着：> **」「记载：\n# 」），
    # 容忍这些标点/标记夹在提示词与引号之间，避免书写内容被误抽成台词。
    r"[：:，,、\s—\-*>＞#`～~。.]*$"
)
# 感知/指称语境：其后引号是被提及/被听到的词语（非台词），留旁白。
_REFERENCE_BEFORE_RE = re.compile(
    r"(听到|听见|听过|想起|想到|提到|提及|讲到|说到|读到|看到|见到|记得|念及|称为|称作|叫做|叫作|唤作|所谓|对于|关于)[：:，,、\s]*$"
)
# 显式说话前缀：行尾「X说道：」「X：」（X 为 2-6 个中文名/称呼），用于把紧邻引号判为台词。
_SAY_PREFIX_RE = re.compile(
    r"([一-龥·]{2,6})(?:说道|说|问道|问|答道|答|开口道|开口|低声道|低声|喊道|叫道|笑道|沉声道|轻声道|道|：|:)[：:，,]?\s*$"
)
_SPEAK_VERB_ALT = (
    r"(?:说道|说|问道|问|答道|答|开口道|开口|低声道|低声|喊道|叫道|笑道|沉声道|轻声道|道)?"
)
# 闭引号「后面」紧跟的说话动词：用于「台词在前、说话人后置」的写法（如『“……”她说』
# 『“……”她回头对你说』）——这类现有「看引号前文」的判定抽不出说话人，会把台词漏成旁白。
# 只收明确的说话动词、去掉单字「道/问/答」等歧义词，降低把「知道/街道」误判成台词的概率。
_TRAILING_SAY_VERB_RE = re.compile(
    r"(说道|说|问道|喊道|叫道|低声道|开口道|沉声道|轻声道|笑道|叹道|回答道|回道|答道|开口)"
)
# 说话人后置且用代词时的兜底署名（判不出具名 NPC 时，用代词也好过把台词混进旁白）。
_PRONOUN_SPEAKERS = ("她", "他", "它", "您", "咱")

# 暗投/暗骰的裁定结果本应「仅 KP 可见」，但模型偶尔会把它错写进方括号泄漏给玩家
# （如 `[暗投结束 - X·心理学检定 失败]`、`【暗骰·NPC·潜行 成功】`）。这类元信息括号一律
# 丢弃、绝不回吐进旁白。匹配：含「暗投/暗骰」，或「检定」紧邻成败判词（含大成功/大失败）。
_BLIND_LEAK_RE = re.compile(
    r"暗投|暗骰|检定[^\[\]【】]{0,8}(大成功|大失败|成功|失败)"
    r"|(大成功|大失败|成功|失败)[^\[\]【】]{0,4}检定"
)


def _strip_speaker_prefix(text: str, speaker: str) -> str:
    """抹掉旁白行尾的「<说话人名>[说道]：」前缀（按完整名/局部名删，长名也不残留半截）。"""
    names = [speaker] + [p for p in speaker.split("·") if len(p) >= 2]
    for nm in sorted(names, key=len, reverse=True):
        new = re.sub(re.escape(nm) + _SPEAK_VERB_ALT + r"[：:，,]?\s*$", "", text)
        if new != text:
            return new
    return text
# 句首/小句边界后充当「主语·动作」的 NPC 名（如「诺特点点头」「史蒂芬转过身」），用于
# 在说话人以代词「他/她」承接、附近又有玩家名时，仍能把台词归给真正在行动的 NPC。
_SUBJECT_BOUNDARY = "。！？!?\n　 ”」』）)】"

CMD_TAG_PREFIXES = (
    "DICE_CHECK:", "OPPOSED_CHECK:", "SAN_CHECK:", "HP_CHANGE:", "NPC_ACT:",
    "SCENE_CHANGE:", "RULE_LOOKUP:", "SET_FLAG:", "CLEAR_FLAG:",
)
# 指令关键词（去冒号）：用于容错识别——模型有时漏写冒号或用空格（如「SET_FLAG hint_x」），
# 也常用全角括号【】。识别到即当指令处理（剔除/执行），避免整条指令泄漏进旁白。
_CMD_TAG_KEYWORDS = tuple(p.rstrip(":") for p in CMD_TAG_PREFIXES)


def _is_cmd_tag(inner: str) -> bool:
    """inner（去掉方括号后的内容）是否为一条终止型指令标签，容忍缺冒号/用空格。"""
    s = inner.lstrip()
    for kw in _CMD_TAG_KEYWORDS:
        if s == kw or s.startswith(kw + ":") or s.startswith(kw + "：") or s.startswith(kw + " "):
            return True
    return False

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

# 「始终暗投」的技能：不管成败等级，结果只回灌 KP、绝不展示给玩家。否则玩家会元游戏——
# 例如看到「心理学失败」就反推出该不该信 NPC。CoC 惯例这类判断由守秘人暗骰。
ALWAYS_BLIND_SKILLS = ("心理学",)

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


def _resolve_scene_ref(module: Module, ref: str) -> str | None:
    """把 SCENE_CHANGE 的引用（场景 id 或场景名，KP 有时会写错/写名字）解析成真实场景 id。

    依次尝试：精确 id → 精确名 → 名字互含 → id 互含。都不中返回 None（调用方据此不改场景，
    避免写入脏 id 后地图回退到第一个场景）。
    """
    ref = (ref or "").strip()
    scenes = (module.scenes if module else []) or []
    if not ref or not scenes:
        return None
    for s in scenes:
        if s.get("id") == ref:
            return ref
    for s in scenes:
        nm = (s.get("name") or s.get("title") or "").strip()
        if nm and nm == ref:
            return s.get("id")
    for s in scenes:
        nm = (s.get("name") or s.get("title") or "").strip()
        if nm and (nm in ref or ref in nm):
            return s.get("id")
    for s in scenes:
        sid = s.get("id") or ""
        if sid and (sid in ref or ref in sid):
            return sid
    return None


def _scene_name(module: Module, scene_id: str) -> str:
    for s in (module.scenes if module else []) or []:
        if s.get("id") == scene_id:
            return s.get("name") or s.get("title") or scene_id
    return scene_id


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
    group_label: str | None = None,
) -> AsyncIterator[str]:
    """流式输出 KP 旁白，并把 NPC 台词抽成对话气泡。

    台词识别两条路：
    1. 显式 ``[SAY: who=<名字>]台词[/SAY]``（最可靠，用于消歧/无名角色/代词承接的说话人）。
    2. 自然引号台词（“”/「」）：据上下文判定说话人——书写/标识/被提及语境一律留旁白；
       否则按「紧邻说话前缀 → 当前说话人(承接) → 最近作为主语行动的 NPC」归属，
       都判不出且附近只有玩家名时，留旁白（不瞎猜）。

    命令标签（[DICE_CHECK] 等）仍终止本次流；[MOVE]/[GROUP] 内联剔除不终止。
    *result* = [narration, full_response, extracted, dialogue_marks, group_marks]。

    ``group_label`` 给定时（分头行动后端按组生成）：本次整段产物确定性地归入该组——
    既给流式 chunk 打上 ``metadata.group``（前端实时分栏），也以 ``(0, label)`` 预置
    group_mark（落库分组），不再依赖模型自觉打 [GROUP]。
    """
    full_response = ""
    narration = ""
    pending = ""
    in_bracket = False
    bracket_buf = ""
    bracket_open = ""   # 记录本次括号是 [ 还是【，非指令时按原样回吐
    tag_found = False

    in_say = False
    say_speaker = ""
    say_buf = ""

    in_quote = False
    quote_open = ""
    quote_buf = ""
    pending_speaker: str | None = None   # 本引号判定出的说话人（None=留旁白）
    pending_weak = False                 # 该说话人是否弱信号（仅靠最近主语推断）
    last_speaker: str | None = None
    written_run = False                  # 处于「一串书写标识引号」中（门牌列表等），后续相邻引号同样留旁白
    gap_since_quote = ""                 # 上一处闭引号至今累计的旁白文本（判断引号是否相邻成串）
    _LIST_SEPS = " 　\t\n、,，;；/和与及·"
    quote_written = False                # 本次引号是否书写/标识内容（据此决定要不要对其做「后置说话人」判定）
    # 「说话人后置」待定台词：闭引号时判不出说话人、但内容像台词，先扣住不落旁白，
    # 看紧跟其后的文字是不是说话动词（"她说"）；是→抽成气泡，否→原样归还旁白。
    deferring = False
    deferred_open = deferred_buf = deferred_close = deferred_tail = ""

    def _looks_like_speech(text: str) -> bool:
        """像台词：有句末标点 / 口语标记 / 够长——用于过滤门牌、招牌等短名词标签。"""
        if len(text) >= 10:
            return True
        return (text and text[-1] in "。！？…?!.~～") or any(
            c in text for c in "你我吗呢吧啊呀嘛！？!?，,"
        )

    npc_matchers: list[tuple[str, list[str], bool]] = []
    for _n in (npcs or []):
        _name = _n.get("name", "")
        if not _name:
            continue
        _parts = [_name]
        for _sep in ("·", "·", " ", "-"):
            if _sep in _name:
                _parts.extend(p.strip() for p in _name.split(_sep) if len(p.strip()) >= 2)
                break
        npc_matchers.append((_name, _parts, bool(_n.get("is_player"))))

    def _canon(name: str) -> str:
        name = (name or "").strip()
        for canonical, parts, _ in npc_matchers:
            if name == canonical or name in parts:
                return canonical
        return name

    def _prefix_speaker(s: str) -> str | None:
        """行尾「X说道：」「X：」→ 说话人（命中已知 NPC 局部名则归一；玩家方角色返回 None 抑制）。"""
        m = _SAY_PREFIX_RE.search(s)
        if not m:
            return None
        name = m.group(1)
        for canonical, parts, is_player in npc_matchers:
            if name == canonical or name in parts or name in canonical:
                return None if is_player else canonical
        # 泛称（护工/老板…）：但排除代词起头与动词短语（如「他开口」），它们不是名字，
        # 交由「最近 NPC 主语」判定真正的说话人（玩家方角色会被那里排除→抑制）。
        if name[0] in "他她它我你咱其这那您" or any(v in name for v in "说道问答开口喊叫笑声"):
            return None
        # 仅当该泛称是「独立称呼」——紧贴小句边界（句首/标点/换行后）才认作说话人；
        # 否则像「他指了指墙上的四个门：」这种以冒号收尾的叙述会把「墙上的四个门」误当名字。
        start = m.start(1)
        if start > 0 and s[start - 1] not in _SUBJECT_BOUNDARY:
            return None
        return name

    def _recent_npc_subject(s: str) -> str | None:
        """最近作为「小句主语」出现的非玩家 NPC（名字紧跟在句首/句末标点后）→ 其后台词的说话人。"""
        recent = s[-200:]
        best_pos, best = -1, None
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                start = 0
                while True:
                    p = recent.find(part, start)
                    if p < 0:
                        break
                    if p == 0 or recent[p - 1] in _SUBJECT_BOUNDARY:
                        if p > best_pos:
                            best_pos, best = p, canonical
                    start = p + 1
        return best

    def _resolve_speaker(pre: str) -> tuple[str | None, bool, bool, bool]:
        """返回 (说话人, 是否弱信号, 是否来自显式前缀, 是否书写内容)。弱信号（仅靠最近 NPC
        主语推断）下，仅当引号文本「像台词」才抽取，避免把门牌/招牌等短名词标签误判为台词。
        from_prefix=True 时，调用方需把「X：」前缀从旁白里抹掉，免得说话人名重复显示。
        is_written=True 表示该引号是书写/标识内容（门牌、招牌、刻字…），留旁白。"""
        s = pre.rstrip()
        if _WRITTEN_TEXT_RE.search(s) or _REFERENCE_BEFORE_RE.search(s):
            return None, False, False, True   # 书写/标识/被提及 → 留旁白
        spk = _prefix_speaker(s)
        if spk:
            return spk, False, True, False    # 强：显式说话前缀
        if last_speaker:
            return last_speaker, False, False, False  # 强：承接当前说话人（段落分隔后会被释放）
        return _recent_npc_subject(s), True, False, False  # 弱：最近行动的 NPC 主语

    def _trailing_speaker(tail: str) -> str | None:
        """从闭引号后的文字（如「，她说」「霍尔护士长低声道」）解析后置说话人：具名优先，
        其次承接 last_speaker / 最近 NPC 主语，最后兜底用代词本身。判不出返回 None。"""
        seg = tail.lstrip("，,。、：: 　\t")
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                if seg.startswith(part):
                    return canonical
        spk = last_speaker or _recent_npc_subject(narration)
        if spk:
            return spk
        return seg[0] if seg and seg[0] in _PRONOUN_SPEAKERS else None

    extracted = result[2]
    dialogue_marks: list = result[3] if len(result) > 3 else []
    group_marks: list = result[4] if len(result) > 4 else []
    # 分头行动按组生成：确定性地把整段归入该组（流式 metadata + 落库 group_mark）。
    if group_label:
        group_marks.append((0, group_label))

    def _mk(chunk_type: str, content: str = "", **kw) -> str:
        """带分组标签的 _make_chunk：group_label 时给 chunk 附 metadata.group，供前端实时分栏。"""
        if group_label:
            md = dict(kw.pop("metadata", None) or {})
            md["group"] = group_label
            kw["metadata"] = md
        return _make_chunk(chunk_type, content, **kw)

    def _flush_pending() -> str | None:
        nonlocal pending, narration
        if not pending:
            return None
        narration += pending
        result[0] = narration
        out = pending if pending.strip() else None
        pending = ""
        return out

    def _emit_say():
        nonlocal in_say, say_speaker, say_buf, last_speaker
        text = say_buf.strip()
        speaker = _canon(say_speaker)
        in_say = False
        say_buf = ""
        say_speaker = ""
        if text and speaker:
            last_speaker = speaker
            extracted.append((speaker, text))
            dialogue_marks.append((len(narration), speaker, text))
            return _mk("npc_dialogue", text, actor_name=speaker)
        return None

    async for token in kp.narrate(messages):
        full_response += token

        for ch in token:
            if deferring:
                deferred_tail += ch
                if _TRAILING_SAY_VERB_RE.search(deferred_tail[:12]):
                    speaker = _trailing_speaker(deferred_tail)
                    text = deferred_buf.strip()
                    if speaker:
                        last_speaker = speaker
                        extracted.append((speaker, text))
                        dialogue_marks.append((len(narration), speaker, text))
                        yield _mk("npc_dialogue", text, actor_name=speaker)
                    else:
                        pending += deferred_open + deferred_buf + deferred_close
                    pending += deferred_tail  # 「她说……」等引导语作旁白
                    deferring = False
                    deferred_tail = ""
                    continue
                if ch == "\n" or len(deferred_tail) > 12:
                    # 后面不是紧邻的说话动词 → 判定非台词，原样归还旁白
                    pending += deferred_open + deferred_buf + deferred_close + deferred_tail
                    deferring = False
                    deferred_tail = ""
                    continue
                continue
            if in_say:
                say_buf += ch
                if say_buf.endswith("[/SAY]"):
                    say_buf = say_buf[:-len("[/SAY]")]
                    chunk = _emit_say()
                    if chunk:
                        yield chunk
                elif say_buf.endswith("\n\n"):
                    say_buf = say_buf[:-2]
                    chunk = _emit_say()
                    if chunk:
                        yield chunk
                continue

            if in_bracket:
                # 容忍全角括号【】：作为与 []] 等价的指令括号闭合
                if ch in "]】":
                    inner_s = bracket_buf.strip()
                    bracket_buf = ""
                    in_bracket = False
                    if inner_s.startswith("MOVE:") or inner_s.startswith("MOVE "):
                        continue
                    if inner_s.startswith("GROUP:") or inner_s.startswith("GROUP "):
                        kv = _parse_tag_kv(inner_s.split(":", 1)[-1] if ":" in inner_s else inner_s[len("GROUP"):])
                        label = (kv.get("scene") or kv.get("group") or kv.get("name") or "").strip()
                        group_marks.append((len(narration), label or None))
                        continue
                    if inner_s.startswith("SAY:") or inner_s.startswith("SAY "):
                        out = _flush_pending()
                        if out:
                            yield _mk("narration", out, actor_name="KP")
                        rest = inner_s[len("SAY"):].lstrip(": ").strip()
                        kv = _parse_tag_kv(rest)
                        say_speaker = (kv.get("who") or kv.get("name") or kv.get("speaker") or rest).strip()
                        say_buf = ""
                        in_say = True
                        continue
                    if _is_cmd_tag(inner_s):
                        tag_found = True
                        break
                    if _BLIND_LEAK_RE.search(inner_s):
                        # 暗投/暗骰裁定结果被 KP 误写进括号 → 丢弃，绝不回吐给玩家
                        continue
                    pending += bracket_open + inner_s + ("】" if bracket_open == "【" else "]")
                else:
                    bracket_buf += ch
            elif ch in "[【":
                in_bracket = True
                bracket_open = ch
                bracket_buf = ""
            elif (ch in "“「『") and not in_quote:
                # 开引号：先判说话人（基于引号前文），冲掉旁白，进入引号收集。
                # 用 narration+pending 作前文：台词常另起一段，此时前文主语（如「诺特」）
                # 已被 flush 进 narration，只看 pending 会漏掉说话人。
                # 「相邻成串」判断：上一处闭引号至今只有分隔符 → 与上一引号同属一串。
                adjacent = gap_since_quote.strip(_LIST_SEPS) == ""
                if not adjacent:
                    written_run = False
                if written_run and adjacent:
                    # 续接书写标识串（如门牌列表）：整串都按书写内容留旁白，不抽台词。
                    pending_speaker, pending_weak, from_prefix = None, False, False
                    quote_written = True
                else:
                    pending_speaker, pending_weak, from_prefix, is_written = _resolve_speaker(narration + pending)
                    written_run = is_written
                    quote_written = is_written
                # 经显式前缀（「史蒂芬·诺特：」）判定说话人时，把该前缀从旁白里抹掉——
                # 否则说话人名会既作旁白文字、又作气泡署名，重复显示。按「完整说话人名」抹，
                # 避免长名（如「加布里埃尔·马卡里奥」）只被删掉后半截、残留「加布里埃」。
                if pending_speaker and from_prefix:
                    pending = _strip_speaker_prefix(pending, pending_speaker)
                out = _flush_pending()
                if out:
                    yield _mk("narration", out, actor_name="KP")
                in_quote = True
                quote_open = ch
                quote_buf = ""
            elif (ch in "”」』") and in_quote:
                in_quote = False
                gap_since_quote = ""        # 闭引号：重置「相邻成串」计数
                text = quote_buf.strip()
                # 弱信号下要求「像台词」，否则按标签/名词留旁白（门牌、招牌等）
                ok = bool(text and pending_speaker) and (not pending_weak or _looks_like_speech(text))
                if ok:
                    last_speaker = pending_speaker
                    extracted.append((pending_speaker, text))
                    dialogue_marks.append((len(narration), pending_speaker, text))
                    yield _mk("npc_dialogue", text, actor_name=pending_speaker)
                elif not quote_written and text and _looks_like_speech(text):
                    # 判不出说话人、但内容像台词：先扣住不落旁白，看紧跟其后的是不是「她说」这类
                    # 后置说话人（说话人在台词之后），是则抽成气泡、否则原样归还旁白。
                    deferring = True
                    deferred_open, deferred_buf, deferred_close = quote_open, quote_buf, ch
                    deferred_tail = ""
                else:
                    pending += quote_open + quote_buf + ch  # 非台词：原样留旁白
                quote_buf = ""
                pending_speaker = None
                pending_weak = False
            else:
                if in_quote:
                    quote_buf += ch
                else:
                    pending += ch
                    gap_since_quote += ch
                    # 段落分隔＝说话的「话筒」交还：清掉 last_speaker，避免上一位说话人
                    # 跨段把后文（如另一场景里读到的报纸短讯）也吸成自己的台词；
                    # 书写标识串也在段落处中断。
                    if pending.endswith("\n\n"):
                        last_speaker = None
                        written_run = False

        if tag_found:
            out = _flush_pending()
            if out:
                yield _mk("narration", out, actor_name="KP")
            break

        if not in_bracket and not in_say and not in_quote and pending:
            while "\n\n" in pending:
                idx = pending.index("\n\n") + 2
                chunk = pending[:idx]
                pending = pending[idx:]
                narration += chunk
                result[0] = narration
                if chunk.strip():
                    yield _mk("narration", chunk, actor_name="KP")
            if len(pending) > 150:
                last_b = -1
                for _i, _ch in enumerate(pending):
                    if _ch in "\n。！？":
                        last_b = _i
                if last_b >= 0:
                    chunk = pending[: last_b + 1]
                    pending = pending[last_b + 1:]
                    narration += chunk
                    result[0] = narration
                    if chunk.strip():
                        yield _mk("narration", chunk, actor_name="KP")

    if not tag_found:
        if in_say:
            chunk = _emit_say()
            if chunk:
                yield chunk
        if in_quote:
            pending += quote_open + quote_buf   # 未闭合引号：留旁白
        if in_bracket:
            pending += (bracket_open or "[") + bracket_buf
        if deferring:
            # 收尾仍在等后置说话人（后面没等到说话动词）：原样归还旁白
            pending += deferred_open + deferred_buf + deferred_close + deferred_tail
        out = _flush_pending()
        if out:
            yield _mk("narration", out, actor_name="KP")

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
    if action not in TEAM_ACTION_EVENT and action not in ("silent", "travel"):
        return None
    return {
        "action": action,
        "content": str(data.get("content") or "").strip(),
        "skill": str(data.get("skill") or "").strip(),   # 仅 action=check 时有意义
        "target": str(data.get("target") or "").strip(),  # 仅 action=travel 时有意义
    }


async def _run_team_turn(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    teammates: list[Character],
    llm,
    blind_results: list[str] | None = None,
) -> AsyncIterator[str]:
    """玩家输入后的一轮 AI 队友自动响应。

    每个队友只决策一次；结果写入事件流，并依次让后续队友 / KP 看到。
    本函数只由 ``run_chat_generation`` / ``run_travel_generation`` 调用，不会自触发，
    故不存在递归链式生成。

    分头判定：队友所在场景 ≠ 主队锚点场景（主角所在）即视为「分头独处」，据此让
    ``build_team_context`` 下达「主动推进本场景」指引；同处一地仍是克制补位。

    ``blind_results``：队友做「始终暗投」技能（如心理学）检定时，真实成败只 append 到这里、
    由调用方注入当轮 KP 上下文，绝不落库/广播——否则玩家能从事件或网络看到结果而元游戏。
    """
    anchor_scene = (
        session_service.get_char_location(game_session, player_char.id)
        or game_session.current_scene_id
    )
    for teammate in teammates[:MAX_TEAMMATES_PER_TURN]:
        events = session_service.get_session_events(db, session_id)
        tm_scene = (
            session_service.get_char_location(game_session, teammate.id)
            or game_session.current_scene_id
        )
        separated = bool(tm_scene and anchor_scene and tm_scene != anchor_scene)
        messages = build_team_context(
            teammate, game_session, module, events, player_char,
            all_teammates=teammates, separated=separated,
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
        # 队友「前往」：显式移动，确定性切换其所在场景（仅限已知地点），落一条「前往」事件。
        # 队友的移动由此动作触发，KP 不再从其台词臆测搬人；分头分组据 party_locations 归并。
        if action == "travel":
            sid = _resolve_scene_ref(module, decision.get("target") or content)
            known = session_service.known_scene_ids(module, game_session, events)
            cur = session_service.get_char_location(game_session, teammate.id)
            if sid and sid in known and sid != cur:
                session_service.set_char_location(db, session_id, teammate.id, sid)
                db.refresh(game_session)
                label = _scene_name(module, sid)
                ev = session_service.add_event(
                    db, session_id, "action", f"（前往：{label}）",
                    actor_id=teammate.id, actor_name=teammate.name,
                )
                yield _make_chunk(
                    "action", f"（前往：{label}）", actor_name=teammate.name,
                    event_id=ev.id, actor_id=teammate.id,
                )
            continue
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
        # 队友主动检定：紧接着掷骰，结果落库交由 KP 收束叙述。心理学等「始终暗投」技能只落
        # 「做了一次暗骰」的事实、结果仅回灌 KP（经 blind_results 注入当轮上下文），绝不落库/
        # 广播成败——否则玩家能从事件或网络看到结果而元游戏。
        if action == "check" and decision.get("skill"):
            skill = decision["skill"]
            engine = get_engine(module.rule_system)
            cdata = {
                "base_attributes": teammate.base_attributes,
                "skills": teammate.skills,
                "system_data": teammate.system_data,
            }
            result = engine.resolve_check(cdata, skill, "normal")
            if any(s in skill for s in ALWAYS_BLIND_SKILLS):
                tier_cn = TIER_LABEL.get(result.tier, result.tier)
                dice_content = f"{teammate.name} 进行了一次暗骰·{skill}（结果仅 KP 可见）"
                dice_meta = {"skill": skill, "actor": teammate.name, "blind": True}
                if blind_results is not None:
                    blind_results.append(
                        f"【暗骰·{teammate.name}·{skill}（结果仅你 KP 可见，绝不可把成败直接告诉玩家）】"
                        f"达成 {tier_cn}：{result.description}"
                    )
            else:
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


def _current_turn_events(events: list) -> list:
    """本回合事件 = 上一段 KP 旁白之后的所有事件（玩家行动 + 本轮队友行动）。"""
    last_narr = -1
    for i, e in enumerate(events):
        if getattr(e, "event_type", None) == "narration":
            last_narr = i
    return events[last_narr + 1:]


def _location_groups(
    game_session: GameSession, module: Module, player_char: Character,
    teammates: list[Character] | None,
) -> list[dict]:
    """按每个队伍成员的「真实所在场景」（party_locations）归并成列 → [{label, members}]。

    分头行动＝队伍成员身处不同场景。位置是确定性状态（玩家经大地图前往、队友经 travel 动作更新），
    故直接据此归并：同场景合一列、列名＝场景名（跨回合稳定）；返回 ≥2 组即为分头。
    不再靠 LLM 猜测分组，也不会因「打算去X」这种意图误判。
    """
    members = [player_char] + list(teammates or [])
    by_scene: dict[str, dict] = {}
    order: list[str] = []
    for ch in members:
        sid = session_service.get_char_location(game_session, ch.id) or game_session.current_scene_id
        if not sid:
            continue
        if sid not in by_scene:
            by_scene[sid] = {"scene_id": sid, "label": _scene_name(module, sid), "members": []}
            order.append(sid)
        by_scene[sid]["members"].append(ch.name)
    return [by_scene[s] for s in order]


SPLIT_FOCUS_PROMPT = (
    "本回合队伍分头行动。现在【只】叙述「{label}」这个场景里发生的事：描写此地的环境、气氛，"
    "以及在场 NPC 对 {members} 言行的反应与由此推进的后续。\n"
    "要求：①详尽完整，与其他分组同等篇幅；②只写这一场景，绝不叙述或提及其他分组的人"
    "（他们另行单独叙述）；③{members} 都是**玩家角色**——**绝不替他们说话、行动、做决定或描写其"
    "心理感受**，只呈现世界与 NPC 对其已有言行的回应。"
)


# 滚动剧情摘要：最近这些事件始终保留全文、不并入摘要；「未并入摘要的事件」超过触发阈值时，
# 才把其中较老的一批与既往摘要合并浓缩一次，推进游标。控制长局上下文规模、防 KP 原地打转。
STORY_SUMMARY_KEEP_RECENT = 12
STORY_SUMMARY_TRIGGER = 24


async def _maybe_roll_story_summary(db: Session, session_id: str, llm) -> None:
    """长局滚动摘要：把「未并入摘要」里较老的一批与既往摘要合并浓缩、推进游标。

    在 KP 每轮生成收尾（done 之后）调用；未攒够阈值时零成本返回，不额外调用 LLM。
    任何失败都静默忽略（保持原摘要、原游标），绝不阻塞跑团。
    """
    try:
        session = db.get(GameSession, session_id)
        if not session:
            return
        events = session_service.get_session_events(db, session_id, limit=0)
        ws = session.world_state or {}
        cursor = ws.get("story_summary_seq") or 0
        uncovered = [e for e in events if (e.sequence_num or 0) > cursor]
        if len(uncovered) <= STORY_SUMMARY_TRIGGER:
            return
        to_summ = uncovered[: len(uncovered) - STORY_SUMMARY_KEEP_RECENT]
        if not to_summ:
            return
        new_summary = await story_summarizer.summarize_story(
            llm, ws.get("story_summary") or "", to_summ,
        )
        if not new_summary:
            return
        ws2 = dict(session.world_state or {})
        ws2["story_summary"] = new_summary
        ws2["story_summary_seq"] = to_summ[-1].sequence_num
        session.world_state = ws2
        db.commit()
        logger.info(
            "滚动剧情摘要更新：session=%s 游标→%s", session_id, to_summ[-1].sequence_num,
        )
    except Exception:
        logger.exception("滚动剧情摘要失败（忽略）: session=%s", session_id)


def _team_blind_message(blind_results: list[str] | None) -> dict | None:
    """把本回合队友暗骰（心理学等）的真实结果打成一条「仅 KP 可见」的 system 消息，注入当轮
    KP 上下文。这些结果绝不落库/广播，只在本次生成的 prompt 里存在——KP 据此把握分寸，
    但绝不可把成败直接告诉玩家。无暗骰则返回 None。"""
    if not blind_results:
        return None
    return {
        "role": "system",
        "content": (
            "【本回合队友暗骰结果——仅你（KP）可见的裁定信息，据此把握分寸叙事，"
            "但绝不可把成败/数值直接告诉玩家】\n" + "\n".join(blind_results)
        ),
    }


async def _validate_and_patch_narration(
    llm, plan: turn_planner.TurnPlan | None, result: list,
) -> None:
    """校验本轮旁白是否违反裁定计划的硬约束（泄露 do_not_reveal / 汇报体+内部标识泄露），
    违反则用改写版本替换落库文本，防止违规内容永久留在会话记录里。

    无法收回已经流式广播出去的内容，但能保证重连、其他玩家、复盘看到的是干净版本。
    只替换 result[0]（落库/展示用的旁白），result[1]（供 _process_commands 解析指令）不动。
    改写后原文的「对话插入偏移」(result[3]) 已失真，落库改走 _persist_narration 的回退路径
    （整段旁白 + 对话追加，牺牲交错顺序换正确性）。
    """
    if plan is None:
        return
    validation = await turn_validator.validate_turn_narration(llm, plan, result[0])
    if validation is None or not validation.violated:
        return
    logger.warning("KP 回合校验发现违规，已改写落库版本：%s", validation.reason)
    result[0] = validation.corrected_narration
    if len(result) > 3:
        del result[3:]


async def _run_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None = None,
    blind_results: list[str] | None = None,
) -> None:
    llm = get_llm()
    kp = KPAgent(llm)
    # 仅在非开场、且该规则系统已挂载规则书时，向 KP 广告 [RULE_LOOKUP] 能力
    rules_enabled = bool(events) and rulebook_service.has_rulebook(db, module.rule_system)
    matcher_npcs = _matcher_npcs(module, teammates)

    # 回合裁定计划：先跑一次结构化 planner（分头与否都需要——分头场景 NPC/线索并行推进，
    # 反而更需要 clue_policy/npc_policy/safety 兜底），失败/开场（无事件）则不注入，KP 走原逻辑。
    plan = None
    if events:
        plan_messages = turn_planner.build_turn_plan_messages(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled,
        )
        plan = await turn_planner.run_turn_planner(llm, plan_messages)

    # 分头行动：按各成员「真实所在场景」归并（玩家经大地图、队友经 travel 动作更新的确定性位置）。
    # 身处 ≥2 个场景即分头 → 逐场景生成叙事。不再靠 LLM 猜分组、也不因「打算去X」误判。
    scene_groups = _location_groups(game_session, module, player_char, teammates)

    # 本回合队友暗骰（心理学等）的真实结果 → 一条「仅 KP 可见」的上下文消息（不落库/不广播）。
    blind_message = _team_blind_message(blind_results)

    if len(scene_groups) >= 2:
        await _run_split_generation(
            db, session_id, game_session, module, player_char, events,
            teammates, kp, llm, rules_enabled, matcher_npcs, scene_groups,
            plan=plan, blind_message=blind_message,
        )
        return

    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=rules_enabled,
    )
    if plan is not None:
        messages.append(turn_planner.build_turn_plan_message(plan))
    if blind_message is not None:
        messages.append(blind_message)

    result = ["", "", [], [], []]
    # 取消（硬取消 task）或流式中途报错（如供应商抖动断流）时，已生成的叙事都要落库，
    # 否则客户端在收到 done 后 resync 会拉到空历史，造成「生成到一半聊天全部消失」。
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, result, npcs=matcher_npcs,
        ):
            room_hub.broadcast(session_id, chunk)
    except BaseException:
        # CancelledError(继承 BaseException) 与普通异常都先把已生成片段落库再上抛
        _persist_narration(db, session_id, result)
        raise
    await _validate_and_patch_narration(llm, plan, result)
    _persist_narration(db, session_id, result)

    async for chunk in _process_commands(
        db, session_id, result[1], module, player_char, game_session, llm,
        teammates=teammates,
    ):
        room_hub.broadcast(session_id, chunk)

    room_hub.broadcast(session_id, _make_chunk("done"))
    await _maybe_roll_story_summary(db, session_id, llm)


def _tag_turn_events_by_group(db: Session, turn_events: list, groups: list[dict]) -> None:
    """把本回合各角色的事件按其所在分组补打 group 标签（玩家行动随其场景列同列）。

    掷骰事件（actor=系统）按其内容里的领头角色名（「亨利·卡特｜…」）归组。
    """
    name_to_group: dict[str, str] = {}
    for g in groups:
        for m in g["members"]:
            name_to_group[m] = g["label"]

    def _match(name: str) -> str | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in name_to_group:
            return name_to_group[name]
        for full, label in name_to_group.items():
            if name in full or full in name:
                return label
        return None

    for e in turn_events:
        etype = getattr(e, "event_type", None)
        if etype not in ("action", "dialogue", "dice"):
            continue
        label = _match(e.actor_name or "")
        if not label and etype == "dice":
            head = re.split(r"[｜|]", e.content or "", 1)[0]
            label = _match(head)
        if label:
            session_service.set_event_group(db, e, label)


async def _run_split_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None,
    kp: KPAgent,
    llm,
    rules_enabled: bool,
    matcher_npcs: list[dict],
    groups: list[dict],
    plan: turn_planner.TurnPlan | None = None,
    blind_message: dict | None = None,
) -> None:
    """分头行动：对每个分组各跑一次聚焦叙事，后端确定性地把产物归入该组。

    每组单独生成 → 篇幅均衡、不会「只详写最后一个场景」；分组标签由后端注入 →
    前端实时/重连都能稳定分栏，不靠模型自觉打 [GROUP]。
    命令（检定/HP/旗标/场景）在所有分组叙事完成后，对合并文本统一处理一次。
    ``plan`` 是本回合唯一的裁定计划（跨分组共用），每组都注入一份、也各自校验一次——
    分头场景 NPC/线索并行推进，同样需要 clue_policy/safety 兜底，不能因为分头
    就退化回纯提示词。
    """
    # 先把本回合各角色的行动/对话/掷骰也归入其所在场景列：这样每一列＝该场景里
    # 「玩家行动 + KP 叙事」自成一体（而非行动全挤在主线、叙事另起一列）。
    # 位置已由显式移动（玩家大地图 / 队友 travel 动作）确定性写入，此处不再据分组反推搬人。
    _tag_turn_events_by_group(db, _current_turn_events(events), groups)
    plan_message = turn_planner.build_turn_plan_message(plan) if plan is not None else None

    combined: list[str] = []
    for grp in groups:
        label = grp["label"]
        members = "、".join(grp["members"])
        # 关键：以该组所在场景为锚构建上下文，否则每列都拿主角场景的 NPC/线索，
        # KP 只能把主角场景重复叙述一遍（两列讲同一件事）。
        messages = build_kp_context(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled, viewer_scene_id=grp.get("scene_id"),
        )
        if plan_message is not None:
            messages.append(plan_message)
        if blind_message is not None:
            messages.append(blind_message)
        messages.append({
            "role": "user",
            "content": SPLIT_FOCUS_PROMPT.format(label=label, members=members),
        })
        result = ["", "", [], [], []]
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, result, npcs=matcher_npcs, group_label=label,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            _persist_narration(db, session_id, result)
            raise
        await _validate_and_patch_narration(llm, plan, result)
        _persist_narration(db, session_id, result)
        combined.append(result[1])

    async for chunk in _process_commands(
        db, session_id, "\n".join(combined), module, player_char, game_session, llm,
        teammates=teammates,
    ):
        room_hub.broadcast(session_id, chunk)

    room_hub.broadcast(session_id, _make_chunk("done"))
    await _maybe_roll_story_summary(db, session_id, llm)


def _skill_names(char: Character) -> list[str]:
    """从角色身上尽可能取出技能名（skills / system_data.skills，兼容 dict 或 list 形态）。"""
    names: set[str] = set()

    def _harvest(obj):
        if isinstance(obj, dict):
            names.update(str(k) for k in obj.keys())
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, dict) and it.get("name"):
                    names.add(str(it["name"]))

    _harvest(getattr(char, "skills", None))
    sd = getattr(char, "system_data", None)
    if isinstance(sd, dict):
        _harvest(sd.get("skills"))
    return sorted(names)


_CHECK_INTENT_SYS = "你是 TRPG 意图分诊器，只输出 JSON，不要解释。"


async def _detect_check_request(llm, text: str, char: Character) -> str | None:
    """轻量意图分诊：玩家本轮是否在【主动申请一次技能/属性检定】。是→返回技能名，否→None。

    单个模型包揽叙事+裁定时，容易把玩家夹带的检定请求当普通叙事顺过去；先分诊出来，直接走
    确定性的检定裁定流程，避免「说了要检定却被无视」。判不准/出错则回退常规叙事（返回 None）。
    """
    skills = _skill_names(char)
    user = (
        f"玩家这轮的输入：\n{text}\n\n"
        + (f"该角色可用技能：{'、'.join(skills)}\n" if skills else "")
        + "判断玩家是否在【主动要求做一次技能/属性检定】"
        "（如「我用心理学看看他说的真假」「我要过一个侦查检定」「掷个聆听」）。\n"
        '是 → {"check": true, "skill": "技能名（尽量用上面列表里的原名）"}\n'
        '否（只是普通说话/行动/移动/环境互动）→ {"check": false}\n只输出 JSON。'
    )
    try:
        raw = await llm.complete(
            [{"role": "system", "content": _CHECK_INTENT_SYS}, {"role": "user", "content": user}],
            temperature=0,
        )
    except Exception:
        logger.exception("意图分诊失败，回退常规流程")
        return None
    data = None
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
    if isinstance(data, dict) and data.get("check"):
        skill = str(data.get("skill") or "").strip()
        return skill or "（未指明技能）"
    return None


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
        llm = get_llm()

        # 意图分诊：玩家本轮是否在申请技能检定？是 → 直接走确定性检定裁定（避免被 KP 当叙事顺过去），
        # 不再跑队友回合与常规叙事。取本轮该玩家的行动/台词文本做判断。
        turn = _current_turn_events(session_service.get_session_events(db, session_id))
        actor_id = next(
            (e.actor_id for e in turn if e.event_type in ("action", "dialogue") and e.actor_id), None,
        )
        acting = (db.get(Character, actor_id) if actor_id else None) or player_char
        player_text = " ".join(
            (e.content or "") for e in turn
            if e.event_type in ("action", "dialogue") and e.actor_id == acting.id and (e.content or "").strip()
        )
        if player_text:
            skill = await _detect_check_request(llm, player_text, acting)
            if skill:
                await _run_kp_turn(
                    db, session_id, game_session, module, player_char, party_others,
                    CHECK_REQUEST_PROMPT.format(actor=acting.name, skill=skill, intent=player_text),
                )
                return

        # 玩家输入后：先跑一轮 AI 队友自动响应（仅 AI 席、仅一轮、不自触发），再交 KP 收束。
        # 队友暗骰（心理学等）的真实结果收集到 team_blind，注入本回合 KP 上下文而不落库/广播。
        team_blind: list[str] = []
        if ai_teammates:
            async for chunk in _run_team_turn(
                db, session_id, game_session, module, player_char, ai_teammates, llm,
                blind_results=team_blind,
            ):
                room_hub.broadcast(session_id, chunk)

        events = session_service.get_session_events(db, session_id)
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others, blind_results=team_blind,
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
    then_team_turn: list[Character] | None = None,
) -> None:
    """跑一轮 KP：注入 user_prompt → 流式叙事 → 处理指令（待定检定/掷骰/场景等）→ done。

    ``then_team_turn`` 给定时（如玩家大地图前往后），在 KP 叙事与指令处理之后、``done`` 之前
    再跑一轮 AI 队友回合——否则这条路（不经 run_chat_generation）的队友永远没有发言机会。
    """
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

    if then_team_turn:
        db.refresh(game_session)  # 叙事里可能有 [SCENE_CHANGE]/[MOVE] 改了位置，重取再判分头
        async for chunk in _run_team_turn(
            db, session_id, game_session, module, player_char, then_team_turn, llm,
        ):
            room_hub.broadcast(session_id, chunk)

    room_hub.broadcast(session_id, _make_chunk("done"))
    await _maybe_roll_story_summary(db, session_id, llm)


async def run_check_request_generation(
    session_id: str, actor_id: str, skill: str, intent: str = "",
) -> None:
    """玩家『申请』检定：交 KP 裁定本次是否需要检定、用什么难度（玩家不指定难度）。

    ``intent`` 是玩家顺带说明的检定目标（如「查书桌暗格」）——现场同时有多条线索/多个
    可疑点时，光报技能名 KP 猜不出具体针对什么，必须带上这句话才能裁定到位。
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
            CHECK_REQUEST_PROMPT.format(
                actor=actor.name, skill=skill,
                intent=intent.strip() or "（未说明，需你结合当前情境自行判断意图）",
            ),
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


def _persist_module_intro(db: Session, session_id: str, module: Module) -> str | None:
    """开场前先落一张「背景导语」卡：模组类型/年代/地区/难度/人数 + 一句话前提。

    取自模组作者填写的公开元信息（world_setting / description），不含任何线索或真相，
    给玩家一个「这是个什么故事」的定位，免得直接被拉进场景而摸不着头脑。返回卡片 chunk。
    """
    ws = module.world_setting or {}
    bits: list[str] = []
    for key in ("tone", "era", "region"):
        v = str(ws.get(key) or "").strip()
        if v:
            bits.append(v)
    diff = str(ws.get("difficulty") or "").strip()
    if diff:
        bits.append(f"难度 {diff}")
    pc = str(ws.get("player_count") or "").strip()
    if pc:
        bits.append(f"建议 {pc} 人")
    meta = " · ".join(bits)
    premise = str(module.description or "").strip()
    if not (meta or premise):
        return None
    ev = session_service.add_event(
        db, session_id, "system", premise, actor_name="系统",
        metadata={"kind": "module_intro", "title": module.title or "模组", "meta": meta},
    )
    return event_to_chunk(ev)


async def run_opening_generation(session_id: str) -> None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        # 幂等：已有正式叙事（旁白/对话）则不重复开场，只收尾。背景导语卡（system）不计入，
        # 这样开场生成中途失败后重试仍能重新生成（而背景卡只补一次、不重复）。
        events_all = session_service.get_session_events(db, session_id)
        if any(e.event_type in ("narration", "dialogue") for e in events_all):
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        if not any((e.metadata_ or {}).get("kind") == "module_intro" for e in events_all):
            intro_chunk = _persist_module_intro(db, session_id, module)
            if intro_chunk:
                room_hub.broadcast(session_id, intro_chunk)
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


async def run_travel_generation(session_id: str, actor_id: str, scene_id: str) -> None:
    """玩家经大地图『前往』某地：确定性切换该角色所在场景，落「前往」行动，再由 KP 叙述抵达。

    场景切换是后端据玩家显式选择执行的（非 KP 臆测），从根上杜绝「说句话就被自动搬走」。
    """
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
        ai_teammates = session_service.get_ai_teammates(db, session_id)
        scene = next((s for s in (module.scenes or []) if s.get("id") == scene_id), None)
        scene_name = (scene or {}).get("title") or (scene or {}).get("name") or scene_id

        room_hub.broadcast(session_id, _make_chunk("generating"))
        # 确定性切换该角色位置（主角则一并更新 current_scene_id），并落一条「前往」行动
        session_service.set_char_location(db, session_id, actor.id, scene_id)
        ev = session_service.add_event(
            db, session_id, "action", f"（前往：{scene_name}）",
            actor_id=actor.id, actor_name=actor.name,
        )
        room_hub.broadcast(session_id, event_to_chunk(ev))

        prompt = (
            f"{actor.name} 抵达了【{scene_name}】。请描述此地此刻的见闻与气氛，自然承接前文；"
            "不要触发任何检定，也不要替其他玩家角色行动或代言。"
        )
        # 前往后紧接一轮 AI 队友回合：留在原地/另处的队友据「分头」处境各自推进本场景，
        # 不再因为这条路不经 run_chat_generation 而全程哑火。
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others, prompt,
            then_team_turn=ai_teammates,
        )
    except asyncio.CancelledError:
        logger.info("前往生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("前往生成失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（前往生成中断，请重试）")
        room_hub.broadcast(session_id, _make_chunk("done"))
    finally:
        db.close()


async def run_regenerate_generation(session_id: str) -> None:
    """重新生成最新一轮 KP 叙事：拿本轮玩家与 AI 队友的既有输入、以及已定的骰子作上下文，
    只重跑 KP（不重跑队友回合、不做检定意图分诊），产出新的叙事。

    调用前应已由端点：①取消卡住的旧生成 task；②回滚上一轮 KP 叙事产物
    （session_service.rollback_last_kp_output）。本函数只负责用清理后的事件流重跑 KP。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        events = session_service.get_session_events(db, session_id)
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others,
        )
    except asyncio.CancelledError:
        logger.info("重新生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("重新生成失败: session=%s", session_id)
        _persist_error_notice(db, session_id, "（重新生成中断，请重试）")
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
    # 全角括号归一为半角：模型有时用【】写指令，统一成 [] 好让下面各指令正则命中并处理（而非泄漏）。
    kp_text = (kp_text or "").replace("【", "[").replace("】", "]")
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
        # 心理学等技能一律强制暗投：即使 KP 写了 visibility=open 或没写，也不挂「待玩家投骰」、
        # 不广播达成等级——结果只回灌 KP，玩家永远看不到成败。
        if any(s in skill_name for s in ALWAYS_BLIND_SKILLS):
            blind = True
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
        ref = match.group(1).strip()
        sid = _resolve_scene_ref(module, ref)
        # 只接受能对应到真实场景的 id/名字；解析不到就不动，
        # 避免写入脏值后地图回退到「第一个场景」造成「玩家换图了地图却没切」。
        old = session_service.get_char_location(game_session, player_char.id)
        if sid and sid != old:
            # 主角明确移动到新场景：更新其位置（→ current_scene_id、已访问、地图跟随）；
            # 同处一地的队友一同前往，分头在别处的队友留在原地。
            session_service.set_char_location(db, session_id, player_char.id, sid)
            for t in (teammates or []):
                if session_service.get_char_location(game_session, t.id) == old:
                    session_service.set_char_location(db, session_id, t.id, sid)
            db.refresh(game_session)
            yield _make_chunk("system", f"场景切换至：{_scene_name(module, sid)}")
        elif not sid:
            logger.warning("SCENE_CHANGE 无法解析场景引用：%r（保持当前场景）", ref)

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
