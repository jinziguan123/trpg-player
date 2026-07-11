from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.ai import story_summarizer, turn_planner, turn_validator
from app.ai import tools as kp_tools
from app.ai.agents import backstage_agent
from app.ai.agents.kp_agent import _CHECK_TURN_TEMPERATURE, KPAgent
from app.ai.agents.npc_agent import NPCAgent
from app.ai.agents.team_agent import TeamAgent
from app.ai.context import build_kp_context, build_npc_context, build_team_context
from app.ai.llm_factory import get_llm
from app.ai.prompts.kp_system import (
    CHECK_REQUEST_PROMPT,
    COMBAT_AFTERMATH_PROMPT,
    KP_DICE_CONTINUATION_PROMPT,
    KP_MODULE_CONTINUATION_PROMPT,
    KP_RULE_CONTINUATION_PROMPT,
)
from app.ai.provider import ToolCall
from app.models.character import Character
from app.models.event_log import EventLog
from app.models.module import Module
from app.models.session import GameSession
from app.rules.registry import get_engine
from app.services import (
    module_rag_service,
    rulebook_service,
    session_service,
    world_memory,
)
from app.services.room_hub import room_hub

logger = logging.getLogger(__name__)

# DICE_CHECK 升级为键值解析（参数顺序无关）：skill=必填；difficulty/char/visibility 选填。
# char=对谁投（空/主角=主角，队友名，NPC 名）；visibility=open|blind（blind=暗投/暗骰，结果只给 KP）。
DICE_CHECK_RE = re.compile(r"\[DICE_CHECK:([^\]]*)\]")
# KP 有时（尤其多人回合）不发 [DICE_CHECK]、而是把「X 检定（normal）：困难成功 (10 ≤ 60)」这类
# **机检结果行**当散文写进旁白——那本是系统掷骰后才产生的内容，KP 自撰＝伪造结果，且玩家看不到
# 投骰提示/动画、结果卡也渲染不出。落库前确定性剥除这类行（要求「检定（<真实难度词>）：<成败等级>」
# 连写，机检签名极强、正常叙事不会出现，误伤概率极低）。配套 kp_system 规则3 的提示词硬约束。
_FAKE_CHECK_RESULT_RE = re.compile(
    r"^[^\n]*?检定（(?:normal|hard|extreme|regular|常规|困难|极难)）\s*[:：]\s*"
    r"(?:大成功|极难成功|困难成功|普通成功|普通失败|大失败|成功|失败)[^\n]*\n?",
    re.M,
)
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
# 模组原文查阅：与 RULE_LOOKUP 同一套终止性指令模式，共享每轮查阅配额。
MODULE_LOOKUP_RE = re.compile(
    r"\[MODULE_LOOKUP:\s*query=([^\]]+)\]"
)
# 剧情状态推进：KP 在叙事节拍发 [SET_FLAG: flag=xxx] 置标志、[CLEAR_FLAG: flag=xxx] 清标志，
# 场景/NPC 的状态变体据此切换（如「地下室进水后变致命」「危险消退」）。是内部控制标签，不展示给玩家。
# 容忍：漏写「flag=」、冒号写成空格（如「[SET_FLAG hint_x]」）。全角括号在处理前已归一为半角。
SET_FLAG_RE = re.compile(r"\[SET_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
CLEAR_FLAG_RE = re.compile(r"\[CLEAR_FLAG[:：\s]\s*(?:flag=)?\s*([^\]]+?)\s*\]")
# 手书发放：KP 在剧情达成发放条件时发 [HANDOUT: id=xxx]，系统把该手书原文以信笺卡片发给全桌。
# 容忍漏写「id=」、冒号写成空格（与 SET_FLAG 同款宽容）。全角括号在处理前已归一为半角。
HANDOUT_RE = re.compile(r"\[HANDOUT[:：\s]\s*([^\]]+?)\s*\]")
# 分头行动：KP 在每个分组/场景内容前标 [GROUP: scene=<场景标签>]，后续内容归该组，前端据此分栏。内联剔除。
# 注：场景瓦片地图已下线，[MOVE]/[MAP_MARK] 不再广告也不再执行；流过滤器仍静默吞掉这两个
# 标签的残余文本形态（见 _stream_narration_filtered 的 startswith 分支），防止泄给玩家。
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
# 不收单字「答」——它几乎只作双字词尾（回答/答话），单收会把「修女在回答」切成「修女在回」+「答」，
# 把动词短语的半截当说话人；真正的答话由「答道/回答」或冒号形式覆盖。
_SAY_PREFIX_RE = re.compile(
    r"([一-龥·]{2,6})(?:说道|说|问道|问|答道|回答|开口道|开口|低声道|低声|喊道|叫道|笑道|沉声道|轻声道|道|：|:)[：:，,]?\s*$"
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
# 含逗号/分号：并列小句的主语常跟在逗号后（「格雷夫斯走进书房，霍尔护士长跟在身后」），
# 漏识别会让多说话人歧义保护（≥2 主语不猜）失效。
_SUBJECT_BOUNDARY = "。！？!?\n　 ”」』）)】，,；;"
# 名字后紧跟这些助词 → 是所有格/枚举（「科比特的…」「科比特、邓宁」），是被谈论的修饰语，
# 不是「在说话的主语」——「最近 NPC 主语」判定时不计入，避免被提及者被当说话人。
_POSSESSIVE_AFTER = "的之、和与及兼或"

CMD_TAG_PREFIXES = (
    "DICE_CHECK:", "OPPOSED_CHECK:", "SAN_CHECK:", "HP_CHANGE:", "NPC_ACT:",
    "SCENE_CHANGE:", "RULE_LOOKUP:", "MODULE_LOOKUP:", "SET_FLAG:", "CLEAR_FLAG:",
    "HANDOUT:",
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

# 单次生成内最多连续查阅的次数（规则书 [RULE_LOOKUP] 与模组原文 [MODULE_LOOKUP]
# 合并计数，防止 KP 反复查导致长链/慢）
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


def _parse_bonus_penalty(kv: dict) -> tuple[int, int]:
    """从指令 kv 解析奖励/惩罚骰数量（缺省 0，非法值按 0，负数取绝对值）。"""
    def _n(key: str) -> int:
        raw = str(kv.get(key) or "").strip()
        try:
            return abs(int(raw)) if raw else 0
        except ValueError:
            return 0
    return _n("bonus"), _n("penalty")


def _check_dice_detail(result) -> dict:
    """把 CheckResult 的逐骰明细组装成前端契约的 dice 对象（kind=check）。

    供 3D 骰子动画严格还原：tens 含所有掷出的十位、tens_kept 是采用值、units 个位、
    bonus/penalty 数量。result 由 tens_kept + units 合成（十位00+个位0=100）。
    """
    return {
        "kind": "check",
        "result": result.roll,
        "tens": list(result.tens),
        "tens_kept": result.tens_kept,
        "units": result.units,
        "bonus": result.bonus,
        "penalty": result.penalty,
    }


def _pool_dice_detail(roll_result) -> dict:
    """把 DiceRollResult（NdM+K 骰池，如 SAN 损失/伤害）组装成契约的 dice 对象（kind=pool）。"""
    sides = 0
    m = re.match(r"\s*\d+d(\d+)", (roll_result.notation or "").strip().lower())
    if m:
        sides = int(m.group(1))
    return {
        "kind": "pool",
        "notation": roll_result.notation,
        "dice": [{"sides": sides, "value": v} for v in roll_result.rolls],
        "modifier": roll_result.modifier,
        "total": roll_result.total,
    }


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


def _present_party(
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """在场玩家角色 = 与主角同处一个场景的 player+teammates（公共/被动群检的默认目标）。

    未追踪位置（单场景游戏常见）→ 全队都在场。分头时只取与主角同场景的一组，
    避免让别处场景的角色也对这里的声响/线索检定。
    """
    party = [player_char] + list(teammates or [])
    locs = session_service.get_party_locations(game_session)
    if not locs:
        return party
    ref = locs.get(player_char.id) or game_session.current_scene_id
    present = [
        c for c in party
        if (locs.get(c.id) or game_session.current_scene_id) == ref
    ]
    return present or party


def _resolve_dice_group_targets(
    char_ref: str,
    group_ref: str,
    game_session: GameSession,
    player_char: Character,
    teammates: list[Character] | None,
) -> list[Character]:
    """群检目标：char=在场/全体 或 chars=在场 → 在场全体；chars=名单 → 具名成员。"""
    ref = (group_ref or char_ref or "").strip()
    if not ref or ref in _ALL_TOKENS or ref.lower() in _ALL_TOKENS:
        return _present_party(game_session, player_char, teammates)
    party = [player_char] + list(teammates or [])
    names = [n.strip() for n in re.split(r"[,，、/]", ref) if n.strip()]
    out: list[Character] = []
    for n in names:
        for c in party:
            if c.name and (c.name == n or n in c.name or c.name in n) and c not in out:
                out.append(c)
    return out or _present_party(game_session, player_char, teammates)


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
              "target": a_res.target, "outcome": a_res.outcome,
              "dice": _check_dice_detail(a_res)},
        "b": {"actor": b_name, "skill": b_skill, "roll": b_res.roll,
              "target": b_res.target, "outcome": b_res.outcome,
              "dice": _check_dice_detail(b_res)},
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


def _matcher_npcs(
    module: Module,
    teammates: list[Character] | None,
    session: GameSession | None = None,
) -> list[dict]:
    """供行内台词归属用的名字表：模组 NPC + 已转正/已登记的临场 NPC + 在场队友（真人/AI）。

    队友不在 module.npcs 里，若不加进来，KP 偶尔替队友写的引号台词会被
    错误归给附近提到的某个模组 NPC（如把约翰·卡特的话记到萨沙·卡纳头上）。
    已转正的临场 NPC 同理并入。**已登记但未转正的临场龙套（管理员/护士长…）也并入**：
    否则它们的台词无名可归，会被在名字表里的某个模组 NPC（甚至已死反派）劫走——即
    「管理员的话被记到沃尔特·科比特头上」。只并入通过合理性校验的名字（滤掉旁白碎片）。
    """
    extra = [{"name": t.name, "is_player": True} for t in (teammates or []) if t.name]
    promoted = world_memory.promoted_npc_cards(session.world_state or {}) if session else []
    improv: list[dict] = []
    if session:
        promoted_names = {c.get("name") for c in promoted}
        for name in (session.world_state or {}).get("improvised_npcs") or {}:
            name = str(name).strip()
            if (name and name not in promoted_names
                    and world_memory.is_plausible_npc_name(name)):
                improv.append({"name": name})
    return (module.npcs or []) + promoted + improv + extra


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


def _narr_quote_span(open_q: str, buf: str, close_q: str) -> str:
    """把「没抽成对话气泡、原样留旁白」的引号片段拼回旁白：剥掉紧贴开/闭引号的换行。

    否则 KP 常写的『台词……\\n”』（闭引号另起一行）会让闭引号在旁白里孤立成一行——
    即用户报的「双引号被分到旁白中」。只剥引号首尾贴着的换行，台词内部换行（多行台词）保留。
    """
    return open_q + buf.strip("\n") + close_q


def _is_party_speaker(name: str, party_names: set[str] | None) -> bool:
    """说话人是否属于玩家党（玩家 + AI 队友）——KP 绝不能用台词气泡替他们说话/行动。

    容忍全名与名字片段互为子串（「伊芙琳」↔「伊芙琳·哈特」）；宁可偶尔挡下一个名字重叠的
    NPC，也不放过「KP 替玩家发声」——后者是最伤的沉浸感杀手。
    """
    if not party_names:
        return False
    n = (name or "").strip()
    if len(n) < 2:
        return False
    for pn in party_names:
        pn = (pn or "").strip()
        if pn and (n == pn or n in pn or pn in n):
            return True
    return False


async def _stream_narration_filtered(
    kp: KPAgent, messages: list[dict], result: list,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    party_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """旧路径入口：KPAgent 流式生成 + 台词过滤（核心逻辑在 _filter_narration_stream）。"""
    async for chunk in _filter_narration_stream(
        kp.narrate(messages), result, npcs=npcs, group_label=group_label,
        party_names=party_names,
    ):
        yield chunk


async def _filter_narration_stream(
    token_stream: AsyncIterator[str], result: list,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    guess_speakers: bool = True,
    party_names: set[str] | None = None,
) -> AsyncIterator[str]:
    """流式输出 KP 旁白，并把 NPC 台词抽成对话气泡。

    ``party_names``（玩家 + AI 队友名）给定时，任何归到玩家党名下的台词都**不生成气泡**——
    KP 绝不能替玩家/队友发声。这是显式 [SAY] 与后置说话人路径缺失的守卫（裸引号路径本就避让玩家党）。

    ``guess_speakers=False``（结构化/say 工具路径）：**关闭裸引号的启发式说话人猜测**——
    无 [SAY] 标记的引号一律留旁白，绝不猜。对话由 say() 工具承担干净的结构化出口，
    故这里不再猜，从根上消灭「归错人」。[SAY] 显式标记仍照常识别（确定性、无歧义）。

    直接消费一个 token 流（与生成来源解耦）：旧路径喂 KPAgent.narrate 的输出，
    agent loop 路径喂 stream_chat 的文本增量——两条路径共用同一套台词抽取/
    指令剔除/流式分段逻辑。

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
    pending_from_prefix = False          # 该说话人是否来自显式「X：」前缀（强信号，内容提名不压制）
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

    def _speaker_named_in_text(speaker: str | None, text: str) -> bool:
        """说话人名字出现在台词内容里 → 多半是「被谈论」而非「在说话」。

        典型：修女谈论科比特（『科比特藏得很深…』），启发式却把台词署名成科比特——
        被谈论者≠说话者。用于压制这类张冠李戴（仅对非显式前缀的弱判定生效）。"""
        if not speaker or not text:
            return False
        for canonical, parts, _ in npc_matchers:
            if canonical == speaker:
                return any(p in text for p in parts)
        return speaker in text

    def _prefix_speaker(s: str) -> str | None:
        """行尾「X说道：」「X：」→ 说话人（命中已知 NPC 局部名则归一；玩家方角色返回 None 抑制）。"""
        m = _SAY_PREFIX_RE.search(s)
        if not m:
            return None
        name = m.group(1)
        for canonical, parts, is_player in npc_matchers:
            if name == canonical or name in parts or name in canonical:
                return None if is_player else canonical
        # 泛称（护工/老板…）：但排除代词起头与动词短语（如「他开口」「修女在回答」），它们不是名字，
        # 交由「最近 NPC 主语」判定真正的说话人（玩家方角色会被那里排除→抑制）。
        # 含「在」= 进行体动词短语（在说/在回答/在念），是动作描写而非说话前缀，一律排除。
        if name[0] in "他她它我你咱其这那您" or any(v in name for v in "说道问答开口喊叫笑声在"):
            return None
        # 仅当该泛称是「独立称呼」——紧贴小句边界（句首/标点/换行后）才认作说话人；
        # 否则像「他指了指墙上的四个门：」这种以冒号收尾的叙述会把「墙上的四个门」误当名字。
        start = m.start(1)
        if start > 0 and s[start - 1] not in _SUBJECT_BOUNDARY:
            return None
        # 合理性校验：挡掉旁白碎片/结构指称被当泛称说话人（「第七节：」「但字距稍疏：」）
        if not world_memory.is_plausible_npc_name(name):
            return None
        return name

    def _recent_npc_subject(s: str) -> str | None:
        """最近作为「小句主语」出现的非玩家 NPC（名字紧跟在句首/句末标点后）→ 其后台词的说话人。

        窗口内出现 **≥2 个不同 NPC 主语**时返回 None：此时「取最近者」≈瞎猜（约一半会归错，
        气泡挂错名字比台词留在旁白更伤沉浸感），宁可留旁白——多说话人场景由 KP 的 [SAY]
        显式指定（prompt 已强制），不靠启发式赌。"""
        recent = s[-200:]
        best_pos, best = -1, None
        subjects: set[str] = set()
        for canonical, parts, is_player in npc_matchers:
            if is_player:
                continue
            for part in parts:
                start = 0
                while True:
                    p = recent.find(part, start)
                    if p < 0:
                        break
                    after = recent[p + len(part): p + len(part) + 1]
                    if p == 0 or recent[p - 1] in _SUBJECT_BOUNDARY:
                        # 计入 subjects（多 NPC 在场 → 触发「≥2 不猜」保护，宁可留旁白）；
                        # 但名字后紧跟所有格/枚举助词（「科比特的遗嘱执行人」「科比特、邓宁」）时是
                        # 被谈论的修饰语/列举，不是「在说话的主语」——不作为返回的说话人。
                        subjects.add(canonical)
                        if after not in _POSSESSIVE_AFTER and p > best_pos:
                            best_pos, best = p, canonical
                    start = p + 1
        if len(subjects) >= 2:
            return None
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
        # 气泡本身即「引号」，KP 若在 [SAY] 内又套了引号（[SAY]“台词”[/SAY]）会让气泡显示成
        # 「“台词」——剥掉首尾包裹的引号；台词内部的引号保留。
        text = say_buf.strip().strip("“”「」『』\"")
        speaker = _canon(say_speaker)
        in_say = False
        say_buf = ""
        say_speaker = ""
        if text and speaker:
            if _is_party_speaker(speaker, party_names):
                return None  # 绝不用气泡替玩家/队友说话：KP 误用 [SAY] 代言 → 丢弃该气泡
            last_speaker = speaker
            extracted.append((speaker, text))
            dialogue_marks.append((len(narration), speaker, text))
            return _mk("npc_dialogue", text, actor_name=speaker)
        return None

    async for token in token_stream:
        full_response += token

        for ch in token:
            if deferring:
                deferred_tail += ch
                if _TRAILING_SAY_VERB_RE.search(deferred_tail[:12]):
                    speaker = _trailing_speaker(deferred_tail)
                    text = deferred_buf.strip()
                    # 后置说话人同样压制「被台词内容点名」的张冠李戴（后置判定从不来自显式前缀）
                    if speaker and _speaker_named_in_text(speaker, text):
                        speaker = None
                    if speaker and _is_party_speaker(speaker, party_names):
                        speaker = None  # 不替玩家/队友发声
                    if speaker:
                        last_speaker = speaker
                        extracted.append((speaker, text))
                        dialogue_marks.append((len(narration), speaker, text))
                        yield _mk("npc_dialogue", text, actor_name=speaker)
                    else:
                        pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close)
                    pending += deferred_tail  # 「她说……」等引导语作旁白
                    deferring = False
                    deferred_tail = ""
                    continue
                if ch == "\n" or len(deferred_tail) > 12:
                    # 后面不是紧邻的说话动词 → 判定非台词，原样归还旁白
                    pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close) + deferred_tail
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
                    if inner_s.startswith("MAP_MARK:") or inner_s.startswith("MAP_MARK "):
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
                if not guess_speakers:
                    # 结构化路径（say() 工具承担对话）：裸引号一律留旁白，绝不启发式猜说话人。
                    pending_speaker, pending_weak, from_prefix = None, False, False
                    quote_written = True
                    written_run = True
                elif written_run and adjacent:
                    # 续接书写标识串（如门牌列表）：整串都按书写内容留旁白，不抽台词。
                    pending_speaker, pending_weak, from_prefix = None, False, False
                    quote_written = True
                else:
                    pending_speaker, pending_weak, from_prefix, is_written = _resolve_speaker(narration + pending)
                    written_run = is_written
                    quote_written = is_written
                pending_from_prefix = from_prefix
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
                # 非显式前缀判定的说话人若被台词内容点名（修女谈论科比特→署名科比特），压制归属
                if ok and not pending_from_prefix and _speaker_named_in_text(pending_speaker, text):
                    ok = False
                if ok and _is_party_speaker(pending_speaker, party_names):
                    ok = False  # 不替玩家/队友发声
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
                    pending += _narr_quote_span(quote_open, quote_buf, ch)  # 非台词：留旁白
                quote_buf = ""
                pending_speaker = None
                pending_weak = False
                pending_from_prefix = False
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
            pending += quote_open + quote_buf.rstrip("\n")   # 未闭合引号：留旁白
        if in_bracket:
            pending += (bracket_open or "[") + bracket_buf
        if deferring:
            # 收尾仍在等后置说话人（后面没等到说话动词）：原样归还旁白
            pending += _narr_quote_span(deferred_open, deferred_buf, deferred_close) + deferred_tail
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
    team_guidance: str = "",
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
            team_guidance=team_guidance,
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
                    "dice": _check_dice_detail(result),
                }
            dev = session_service.add_event(
                db, session_id, "dice", dice_content,
                actor_name="系统", metadata=dice_meta,
            )
            yield _make_chunk("dice", dice_content, metadata=dice_meta, event_id=dev.id)
            # 世界记忆钩子 d：队友暗投若能确定性归属到唯一 NPC（行动描述里恰好点名一个），
            # 记录该 NPC「被看穿/未被看穿」；归属不成立则跳过，绝不猜测。
            if dice_meta.get("blind"):
                target = _match_single_npc(module, content)
                if target:
                    seen_through = result.outcome in (
                        "critical_success", "hard_success", "success",
                    )
                    verdict = "看穿" if seen_through else "试探，但未被看穿"
                    _apply_world_memory(
                        db, game_session,
                        lambda ws: world_memory.record_npc_interaction(
                            ws, target[0], dev.sequence_num,
                            f"被 {teammate.name} 用{skill}{verdict}",
                        ),
                    )


def _persist_error_notice(db: Session, session_id: str, text: str) -> None:
    """把生成中断提示落库为 system 事件，保证在客户端 resync 后仍可见。"""
    try:
        session_service.add_event(db, session_id, "system", text, actor_name="系统")
    except Exception:
        logger.exception("落库生成中断提示失败: session=%s", session_id)


def _classify_llm_error(exc: BaseException) -> str:
    """把底层异常翻成对玩家可行动的一句话（鉴权/限流/网络）；无法归类返回空串。"""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "鉴权失败，请到设置页检查 API Key 是否正确并已激活"
        if code == 429:
            return "被限流或额度不足，请稍后重试或检查账户额度"
        if code >= 500:
            return "AI 服务端错误，通常稍后重试即可"
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.ReadTimeout):
        return "连接 AI 服务失败，请检查网络或设置页的 base_url"
    return ""


# 只由引号字符（±空白）组成的整行——KP 把闭引号写在台词/[SAY] 之外时留下的「孤立引号行」，
# 渲染成旁白里孤零零的一个 ” / “（用户报的「双引号被分到旁白中」）。落库前整行剥除。
_ORPHAN_QUOTE_LINE_RE = re.compile(r"(?m)^[ \t　]*[“”「」『』\"]+[ \t　]*(?:\n|$)")


def _strip_marked_lines(narration: str, marks: list | None, regex) -> tuple[str, list | None]:
    """按 regex 整段剥除旁白里的问题行，并同步修正 dialogue_marks 偏移：删除点之前的 mark
    不动，之后的按累计删除量前移（落在被删区间内的 mark 挪到区间起点）。"""
    if not regex.search(narration):
        return narration, marks
    removals = [(m.start(), m.end()) for m in regex.finditer(narration)]
    new_narr = regex.sub("", narration)
    if not marks:
        return new_narr, marks

    def _shift(off: int) -> int:
        removed = 0
        for s, e in removals:
            if e <= off:
                removed += e - s
            elif s < off < e:
                removed += off - s
        return off - removed

    return new_narr, [(_shift(o), spk, txt) for o, spk, txt in marks]


def _persist_narration(
    db: Session, session_id: str, result: list, event_order: list | None = None,
) -> None:
    """落库 KP 这一轮产物，保留旁白与对话的交错顺序（与流式渲染一致）。

    用 result[3] 里记录的「对话插入偏移」把整段旁白切开、与对话交错落库；
    没有偏移信息（旧调用）时回退为「旁白整段在前、对话在后」。

    ``event_order``（tool-loop 传入）给定时，把每条新建事件的 (offset, id) 追加进去，
    供收尾 _reorder_turn_events 把「loop 内即时落库的工具事件」与旁白按广播顺序重排。
    """
    narration = result[0]
    marks = result[3] if len(result) > 3 else None
    narration, marks = _strip_marked_lines(narration, marks, _FAKE_CHECK_RESULT_RE)
    narration, marks = _strip_marked_lines(narration, marks, _ORPHAN_QUOTE_LINE_RE)
    group_marks = sorted(result[4], key=lambda g: g[0]) if len(result) > 4 and result[4] else []

    def _group_at(offset: int) -> str | None:
        g = None
        for off, label in group_marks:
            if off <= offset:
                g = label
            else:
                break
        return g

    def _record(ev, offset: int) -> None:
        if event_order is not None and ev is not None:
            event_order.append((offset, ev.id))

    def _add_narr(text: str, offset: int) -> None:
        t = text.rstrip()
        if t:
            ev = session_service.add_event(
                db, session_id, "narration", t, actor_name="KP", group=_group_at(offset),
            )
            _record(ev, offset)

    # loop 事件的广播偏移（event_order 里此刻仅有的条目）也作为**切分点**——只切开旁白，
    # 事件本身已在 loop 内落库，收尾重排会把它插回该偏移处；否则整段旁白不切、工具事件无处可插。
    loop_cuts = {off for off, _id in (event_order or [])}

    dialogue_at: dict[int, list[tuple[str, str]]] = {}
    if marks:
        for off, npc_name, dialogue_text in marks:
            dialogue_at.setdefault(off, []).append((npc_name, dialogue_text))

    if marks is not None or loop_cuts:
        cuts = sorted(set(dialogue_at) | loop_cuts)
        pos = 0
        for off in cuts:
            off = max(pos, min(off, len(narration)))
            if off > pos:
                _add_narr(narration[pos:off], pos)
            for npc_name, dialogue_text in dialogue_at.get(off, []):
                if dialogue_text:
                    ev = session_service.add_event(
                        db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
                        group=_group_at(off),
                    )
                    _record(ev, off)
            pos = off
        _add_narr(narration[pos:], pos)
        return

    # 回退：无交错信息
    _add_narr(narration, 0)
    for npc_name, dialogue_text in result[2]:
        ev = session_service.add_event(
            db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
        )
        _record(ev, len(narration))


def _record_chunk_event(event_order: list, chunk: str, offset: int) -> None:
    """把一条广播 chunk 对应的「已落库事件」记入重排清单：(此刻旁白长度作偏移, 事件 id)。
    只记带 id 的持久事件（骰子/检定请求/NPC 台词/HP 变化等 loop 内即时落库的展示事件）。"""
    try:
        data = json.loads(chunk[len("data: "):]) if chunk.startswith("data: ") else None
    except (ValueError, TypeError):
        return
    if data and data.get("id"):
        event_order.append((offset, data["id"]))


def _reorder_turn_events(
    db: Session, session_id: str, event_order: list, base_seq: int
) -> None:
    """按广播顺序（偏移）重排本轮所有展示事件的 sequence_num，使 resync 顺序 == 直播顺序。

    tool-loop 里工具事件（骰子/检定/NPC 台词…）在 loop 内即时落库、拿到较小序号，而旁白在收尾
    才落库、序号更大——resync 后它们会被甩到旁白前面/成堆。本函数把本轮（seq > base_seq）的
    这些事件按偏移稳定排序后，重写为连续序号，恢复「旁白→骰子→旁白→台词」的交错。
    """
    if not event_order:
        return
    # 稳定按偏移排序；同偏移保持捕获顺序（loop 内事件先于收尾旁白追加，≈广播先后）
    order: list[str] = []
    seen: set[str] = set()
    for _off, eid in sorted(event_order, key=lambda m: m[0]):
        if eid not in seen:
            seen.add(eid)
            order.append(eid)
    seq = base_seq
    for eid in order:
        ev = db.get(EventLog, eid)
        if ev is not None and (ev.sequence_num or 0) > base_seq:
            seq += 1
            ev.sequence_num = seq
    db.commit()


def _current_turn_events(events: list) -> list:
    """本回合事件 = 上一段 KP 旁白之后的所有事件（玩家行动 + 本轮队友行动）。"""
    last_narr = -1
    for i, e in enumerate(events):
        if getattr(e, "event_type", None) == "narration":
            last_narr = i
    return events[last_narr + 1:]


def commit_pending_travel(db: Session, session_id: str, turn: list | None = None) -> None:
    """把本回合已转正的『前往』动作落成确定性位置同步。

    大地图暂存式前往（stash=True）只记一条带 ``travel``/``scene_id`` 元数据的 pending_turn 动作；
    推进本回合后本函数在建 KP 上下文前把对应角色搬到目标场景，KP 随即以正确位置叙述抵达见闻。
    """
    if turn is None:
        turn = _current_turn_events(session_service.get_session_events(db, session_id))
    for ev in turn:
        meta = ev.metadata_ or {}
        if meta.get("travel") and meta.get("scene_id") and ev.actor_id:
            session_service.set_char_location(db, session_id, ev.actor_id, meta["scene_id"])


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
    "心理感受**，只呈现世界与 NPC 对其已有言行的回应；"
    "④**此地的 NPC 对其他分组在别处的言行一无所知**（除非大到隔墙可闻的巨响、或有人当面告知）——"
    "绝不让 NPC 评论、追问或以任何方式反应它感知之外的事。"
)


# 滚动剧情摘要：最近这些事件始终保留全文、不并入摘要；「未并入摘要的事件」超过触发阈值时，
# 才把其中较老的一批与既往摘要合并浓缩一次，推进游标。控制长局上下文规模、防 KP 原地打转。
STORY_SUMMARY_KEEP_RECENT = 12
STORY_SUMMARY_TRIGGER = 24


async def _maybe_roll_story_summary(db: Session, session_id: str, llm) -> None:
    """长局滚动摘要 + 世界记忆抽取（v2）：把「未并入摘要」里较老的一批与既往摘要合并浓缩、
    推进游标，**同一次低温调用**顺带产出 NPC 记忆/线索备注的差量并合并落库。

    在 KP 每轮生成收尾（done 之后）调用；未攒够阈值时零成本返回，不额外调用 LLM。
    摘要失败：静默忽略（保持原摘要、原游标）。抽取差量失败/为空：跳过差量、摘要照常推进；
    差量落库经 ``_apply_world_memory`` fail-open，且台账 status 恒不受抽取器影响。绝不阻塞跑团。
    """
    try:
        session = db.get(GameSession, session_id)
        if not session:
            return
        events = session_service.get_session_events(db, session_id, limit=0)
        ws = session.world_state or {}
        cursor = ws.get("story_summary_seq") or 0
        # 幕后事件（仅 KP 可见）不进剧情摘要：摘要以「玩家已经历的剧情」口吻注入后续
        # 所有 KP 上下文，混入会让幕后内容脱离「幕后动态」小节的守密措辞（格式混入风险）；
        # KP 本就能从专属小节看到幕后动态，摘要里无需重复。
        uncovered = [
            e for e in events
            if (e.sequence_num or 0) > cursor
            and not session_service.is_kp_only_event(e)
        ]
        if len(uncovered) <= STORY_SUMMARY_TRIGGER:
            return
        to_summ = uncovered[: len(uncovered) - STORY_SUMMARY_KEEP_RECENT]
        if not to_summ:
            return
        # MemoryKeeper 抽取器与摘要合并为一次调用（同一批事件 + 既往摘要 + 当前 NPC 记忆摘要）
        module = db.get(Module, session.module_id) if session.module_id else None
        npc_names = {
            npc.get("id"): npc.get("name")
            for npc in ((module.npcs if module else None) or [])
            if npc.get("id")
        }
        # 叙事主流已停但仍持锁做收尾：给前端一个可读状态，别让玩家对着无声脉冲点干等。
        room_hub.broadcast(session_id, _make_chunk("housekeeping", "KP 正在整理笔记…"))
        result = await story_summarizer.summarize_and_extract(
            llm, ws.get("story_summary") or "", to_summ,
            world_memory.format_npc_memory_all_brief(ws, npc_names),
        )
        if not result:
            return
        new_summary, npc_updates, clue_notes = result
        ws2 = dict(session.world_state or {})
        ws2["story_summary"] = new_summary
        ws2["story_summary_seq"] = to_summ[-1].sequence_num
        session.world_state = ws2
        db.commit()
        # 差量合并：只改 attitude/reason/promises/lies 与已存在线索的 note，绝不碰台账 status。
        if npc_updates or clue_notes:
            _apply_world_memory(
                db, session,
                lambda w: world_memory.apply_memory_delta(w, npc_updates, clue_notes),
            )
        logger.info(
            "滚动剧情摘要更新：session=%s 游标→%s", session_id, to_summ[-1].sequence_num,
        )
    except Exception:
        logger.exception("滚动剧情摘要失败（忽略）: session=%s", session_id)


# 幕后推演触发间隔：自游标（world_state.backstage.last_run_seq）起累计的「玩家回合」
# 事件数（玩家方角色的 action/dialogue，近似计数——与导演信号的判定思路一致）。
BACKSTAGE_TURN_INTERVAL = 6
# validator 预筛：最多把最近几条幕后事件文本挂进 plan.safety.do_not_reveal
BACKSTAGE_DO_NOT_REVEAL_MAX = 3


async def _maybe_run_backstage(db: Session, session_id: str, llm) -> None:
    """幕后推演（Backstage Clock）：让世界在玩家不在场时按 NPC 的动机演进。

    触发（在 KP 每轮生成收尾处评估，不阻塞叙事主流程）：
    - 自游标起累计 ≥ ``BACKSTAGE_TURN_INTERVAL`` 个玩家回合事件；或
    - 场景已切换（``backstage.last_scene_id`` ≠ 当前场景，[SCENE_CHANGE] 的天然时间流逝点）。
    模组无带 secrets/goals 的 NPC → 永不触发、零调用；条件不满足 → 零成本返回。

    安全约束（最重要）：幕后事件绝不直接改 flags / 剧情状态——只落 event_logs
    （``visibility=["kp"]``，不广播，玩家永远不可见）+ 注入 KP 上下文；
    ``suggest_flags`` 只是给 KP 的建议，是否 [SET_FLAG] 由 KP 后续叙事决定。

    fail-open：LLM 异常 / 坏 JSON → 游标不动、无事件落库、下轮重试；任何异常不上抛。
    """
    try:
        session = db.get(GameSession, session_id)
        if not session or not session.module_id:
            return
        module = db.get(Module, session.module_id)
        if not module:
            return
        secret_npcs = backstage_agent.npcs_with_secrets(module)
        if not secret_npcs:
            return  # 模组无幕后主体：永不触发，零调用
        ws = session.world_state or {}
        bs = dict(ws.get("backstage") or {})
        if not bs:
            # 首次评估：立基线（游标 0 + 当前场景），此后场景切换才可比对触发
            _apply_world_memory(
                db, session,
                lambda w: world_memory.advance_backstage_cursor(
                    w, 0, session.current_scene_id,
                ),
            )
            bs = {"last_run_seq": 0, "last_scene_id": session.current_scene_id}
        cursor = int(bs.get("last_run_seq") or 0)
        events = session_service.get_session_events(db, session_id)
        if not events:
            return
        last_seq = int(events[-1].sequence_num or 0)
        if last_seq <= cursor:
            return
        party_ids = {session.player_character_id} | {
            c.id for c in session_service.get_party_members(db, session_id)
        }
        turns = sum(
            1 for e in events
            if (e.sequence_num or 0) > cursor
            and e.event_type in ("action", "dialogue")
            and e.actor_id in party_ids
        )
        last_scene = bs.get("last_scene_id")
        scene_changed = bool(
            last_scene and session.current_scene_id
            and session.current_scene_id != last_scene
        )
        if turns < BACKSTAGE_TURN_INTERVAL and not scene_changed:
            return

        since = [e for e in events if (e.sequence_num or 0) > cursor]
        messages = backstage_agent.build_backstage_messages(
            session, module, secret_npcs, since,
        )
        # 幕后推演刻意不广播任何 chunk（含状态提示）——幕后是「仅 KP 可见」的隔离机制，
        # 连「正在推演幕后」这类信号都不外泄（见 test_backstage 的广播禁令）。收尾期的可读
        # 状态由 _maybe_roll_story_summary 的「整理笔记」承担；此处保持静默。
        agent = backstage_agent.BackstageAgent(llm)
        valid_ids = {n.get("id") for n in (module.npcs or []) if n.get("id")}
        bevents = await agent.infer(messages, valid_ids)
        if bevents is None:
            return  # 调用/解析失败：游标不动、不落库（fail-open，下轮重试）

        npc_names = {
            n.get("id"): n.get("name") or n.get("id")
            for n in (module.npcs or []) if n.get("id")
        }
        for be in bevents:
            content = f"{npc_names.get(be['npc_id'], be['npc_id'])}：{be['action']}"
            if be.get("affected_scene"):
                content += f"（涉及：{_scene_name(module, be['affected_scene'])}）"
            # 只落库，不广播（幕后事件玩家永远不可见，无 UI）
            session_service.add_event(
                db, session_id, "system", content, actor_name="幕后",
                visibility=[session_service.KP_ONLY_SENTINEL],
                metadata={
                    "kind": "backstage",
                    "npc_id": be["npc_id"],
                    "affected_scene": be.get("affected_scene") or "",
                    "suggest_flags": be.get("suggest_flags") or [],
                },
            )
        # 推演成功（含 0 条＝「无事发生」也是结果）：游标推进到评估时的最新事件序号
        _apply_world_memory(
            db, session,
            lambda w: world_memory.advance_backstage_cursor(
                w, last_seq, session.current_scene_id,
            ),
        )
        logger.info(
            "幕后推演完成：session=%s 事件=%d 游标→%s", session_id, len(bevents), last_seq,
        )
    except Exception:
        logger.exception("幕后推演失败（忽略）: session=%s", session_id)


async def _finish_generation(db: Session, session_id: str, llm) -> None:
    """生成收尾：先跑完会话级 housekeeping（滚动摘要 + 幕后推演，二者都写 world_state 且仍
    持有本次生成锁 is_generating=True），**再**广播 done。

    顺序很关键：若把 done 放在 housekeeping 之前，玩家会看到「KP 已不再吐字」（done 到达、
    streaming 置 false）却因 is_generating 仍为 True（housekeeping 的 LLM 调用还在跑）而投骰/
    申请检定被后端 409「KP 正在叙事」——这正是线上「明明不吐字了还显示 KP 叙事中」的成因。
    housekeeping 通常是零调用（未达摘要阈值 / 模组无幕后主体），此时 done 与今日一样即时。"""
    await _maybe_roll_story_summary(db, session_id, llm)
    # 幕后推演：KP 回合收尾处评估（不阻塞叙事主流程；条件不满足零调用）
    await _maybe_run_backstage(db, session_id, llm)
    room_hub.broadcast(session_id, _make_chunk("done"))


def _augment_plan_with_backstage(plan: turn_planner.TurnPlan | None, events: list) -> None:
    """validator 预筛：把最近的幕后事件文本挂进 ``plan.safety.do_not_reveal``。

    选它作为「预筛清单加幕后文本」的最小侵入实现：do_not_reveal 非空会让
    ``turn_validator._looks_suspicious`` 判定值得校验（KP 直接复述幕后事件即被
    改写拦下），且校验器把这些文本当硬性隐藏信息，连转述/暗示式泄露也能兜住；
    turn_validator 本身零改动。代价是幕后事件存续期间每轮多一次低温校验调用，可接受。
    KP 自身也会在计划消息里看到这份 do_not_reveal，等于再叮嘱一次守密。
    """
    if plan is None:
        return
    texts = [
        (e.content or "").strip()
        for e in (events or [])
        if (e.metadata_ or {}).get("kind") == "backstage" and (e.content or "").strip()
    ]
    for text in texts[-BACKSTAGE_DO_NOT_REVEAL_MAX:]:
        entry = "幕后事件（玩家不可见，绝不复述或暗示）：" + text[:80]
        if entry not in plan.safety.do_not_reveal:
            plan.safety.do_not_reveal.append(entry)


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


def _apply_world_memory(db: Session, game_session: GameSession, mutate) -> None:
    """把一次世界记忆更新（world_memory 的纯函数）落到 world_state。

    JSON 列必须整 dict 重新赋值才会被 SQLAlchemy 追踪；记忆是增强件，
    任何异常只记日志、回滚后继续——绝不允许阻塞跑团主流程。
    """
    try:
        ws = mutate(dict(game_session.world_state or {}))
        if not isinstance(ws, dict):
            return
        game_session.world_state = ws
        db.add(game_session)
        db.commit()
        db.refresh(game_session)
    except Exception:
        logger.exception(
            "世界记忆更新失败（忽略）: session=%s", getattr(game_session, "id", "?"),
        )
        try:
            db.rollback()
        except Exception:
            pass


def _match_single_npc(module: Module, text: str) -> tuple[str, str] | None:
    """在自由文本里按 NPC 名做子串匹配：恰好唯一命中才返回 (npc_id, name)，否则 None。

    用于暗投（心理学等）的目标归属：归属必须确定性成立，宁缺毋滥——
    零命中或多命中一律放弃，不做任何猜测。
    """
    text = (text or "").strip()
    if not text:
        return None
    hits: list[tuple[str, str]] = []
    for npc in (module.npcs if module else []) or []:
        nid = npc.get("id")
        name = (npc.get("name") or "").strip()
        if not nid or not name:
            continue
        parts = [name] + [p.strip() for p in name.split("·") if len(p.strip()) >= 2]
        if any(p in text for p in parts):
            hits.append((nid, name))
    return hits[0] if len(hits) == 1 else None


def _record_clue_ledger_from_plan(
    db: Session,
    game_session: GameSession,
    plan: turn_planner.TurnPlan,
    events: list,
    player_char: Character,
    teammates: list[Character] | None,
) -> None:
    """世界记忆钩子 a：planner 裁定本轮揭示线索（reveal_level != none 且有 candidate）
    即写入台账（partial ← hint，known ← direct）。

    discovered_by 取「与主角同场景」的玩家角色——分头行动下另一队并不知情，信息不共享。
    """
    policy = plan.clue_policy
    if not policy.candidate_clue_ids:
        return
    if world_memory.reveal_status(policy.reveal_level) is None:
        return
    anchor = session_service.get_char_location(game_session, player_char.id)
    present = [player_char.id]
    for t in teammates or []:
        if session_service.get_char_location(game_session, t.id) == anchor:
            present.append(t.id)
    seq = 0
    for e in reversed(events or []):
        if getattr(e, "sequence_num", None):
            seq = e.sequence_num
            break
    _apply_world_memory(db, game_session, lambda ws: world_memory.record_clue_reveal(
        ws, policy.candidate_clue_ids, policy.reveal_level, present, seq,
        note=policy.notes,
    ))


def _record_npc_say_memory(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    speaker_texts: list,
    audience_names: list[str],
) -> None:
    """世界记忆钩子 c：本轮落库的 NPC 台词（[SAY]/引号抽取）记入该 NPC 的互动史
    ——「对谁说了话」。同时登记**临场 NPC**（模组未列出的开口龙套）供收容机制约束。

    只认得出 module.npcs 的说话人（队友台词不入 NPC 记忆）；同一 NPC 一轮只记一条，
    防止多句台词灌爆环形缓冲。说话人不在 module.npcs、也不是玩家角色/系统 → 视为临场 NPC，
    登记进 world_state.improvised_npcs（详见临场 NPC 收容设计）。
    """
    if not speaker_texts:
        return
    # 正典说话人 = 模组 NPC + 本会话已转正的临场 NPC（后者转正后开始有 npc_memory、不再算龙套）
    _npc_defs = ((module.npcs if module else []) or []) + world_memory.promoted_npc_cards(
        game_session.world_state or {}
    )
    by_name = {
        (npc.get("name") or "").strip(): npc.get("id")
        for npc in _npc_defs
        if npc.get("id") and npc.get("name")
    }
    # 玩家侧名单 + 系统/KP：这些说话人不算临场 NPC（audience_names 即玩家+队友名）
    _non_npc = {n.strip() for n in (audience_names or []) if n and n.strip()}
    _non_npc |= {"系统", "KP", "旁白"}
    picked: dict[str, str] = {}
    improv_names: list[str] = []
    for speaker, text in speaker_texts:
        sp = (speaker or "").strip()
        if not sp or not str(text or "").strip():
            continue
        nid = by_name.get(sp)
        if nid:
            if nid not in picked:
                picked[nid] = str(text).strip()
        elif sp not in _non_npc and sp not in improv_names:
            improv_names.append(sp)   # 非正典、非玩家、非系统 → 临场龙套
    if not picked and not improv_names:
        return
    try:
        evs = session_service.get_session_events(db, session_id)
        seq = (evs[-1].sequence_num or 0) if evs else 0
    except Exception:
        seq = 0
    audience = "、".join(n for n in (audience_names or []) if n) or "在场众人"
    for nid, text in picked.items():
        _apply_world_memory(
            db, game_session,
            lambda ws, _nid=nid, _text=text: world_memory.record_npc_interaction(
                ws, _nid, seq, f"对{audience}说：{_text[:40]}",
            ),
        )
    for name in improv_names:
        _apply_world_memory(
            db, game_session,
            lambda ws, _name=name: world_memory.record_improvised_npc(ws, _name, seq),
        )


def _snap_offset(text: str, off: int) -> int:
    """把偏移吸附到最近的句末/换行边界，避免在句子中间插入对话气泡（就近向后、再向前找）。"""
    n = len(text)
    off = max(0, min(off, n))
    if off <= 0 or off >= n:
        return off
    for i in range(off, min(off + 40, n)):
        if text[i] in "。！？…\n":
            return i + 1
    for i in range(off, max(off - 40, 0), -1):
        if text[i] in "。！？…\n":
            return i + 1
    return off


def _remap_marks_after_rewrite(
    result: list, old_narr: str, event_order: list | None = None,
) -> None:
    """旁白被校验改写后，把 result[3]（对话交错偏移）/result[4]（分组偏移）及 event_order
    （tool-loop 事件的广播偏移）按长度比例重映射到新文本并吸附到句界——**保住交错顺序**，
    气泡/工具事件仍插在对应旁白之后，而非全部堆到末尾。"""
    new_narr = result[0]
    old_len = len(old_narr)
    if old_len <= 0:
        if len(result) > 3:
            del result[3:]
        if event_order is not None:
            event_order[:] = [(0, eid) for _o, eid in event_order]
        return
    scale = len(new_narr) / old_len

    def _remap(off: int) -> int:
        return _snap_offset(new_narr, int(round(off * scale)))

    if len(result) > 3 and result[3]:
        result[3] = [(_remap(o), spk, txt) for (o, spk, txt) in result[3]]
    if len(result) > 4 and result[4]:
        result[4] = [(_remap(o), label) for (o, label) in result[4]]
    if event_order:
        event_order[:] = [(_remap(o), eid) for (o, eid) in event_order]


async def _validate_and_patch_narration(
    llm, plan: turn_planner.TurnPlan | None, result: list,
    event_order: list | None = None,
) -> None:
    """校验本轮旁白是否违反裁定计划的硬约束（泄露 do_not_reveal / 汇报体+内部标识泄露），
    违反则用改写版本替换落库文本，防止违规内容永久留在会话记录里。

    无法收回已经流式广播出去的内容，但能保证重连、其他玩家、复盘看到的是干净版本。
    只替换 result[0]（落库/展示用的旁白），result[1]（供 _process_commands 解析指令）不动。
    改写会使 result[3]（对话交错偏移）相对原文失真——**不再直接丢弃**（那会让 _persist_narration
    走「整段旁白 + 对话全部追加」的回退，旁白与气泡各自成堆、丢交错顺序，是用户可见的渲染 bug），
    改为按长度比例重映射偏移，保住交错顺序。
    """
    if plan is None:
        return
    validation = await turn_validator.validate_turn_narration(llm, plan, result[0])
    if validation is None or not validation.violated:
        return
    logger.warning("KP 回合校验发现违规，已改写落库版本：%s", validation.reason)
    old_narr = result[0]
    result[0] = validation.corrected_narration
    _remap_marks_after_rewrite(result, old_narr, event_order)


def _scene_title(module: Module, scene_id: str | None) -> str:
    """按 id 取场景标题（title/name 兼容），找不到返回空串。"""
    for s in (module.scenes or []):
        if s.get("id") == scene_id:
            return str(s.get("title") or s.get("name") or "")
    return ""


def _latest_player_input(events: list, party_char_ids: set[str]) -> str:
    """玩家一侧（含队友）最新的一条发言/行动文本，作为被动检索 query 的一半。"""
    for ev in reversed(events or []):
        if (
            ev.event_type in ("action", "dialogue")
            and ev.actor_id
            and ev.actor_id in party_char_ids
        ):
            return ev.content or ""
    return ""


def _module_excerpts_for_context(
    db: Session,
    module: Module,
    game_session: GameSession,
    events: list,
    party_char_ids: set[str],
    scene_id: str | None = None,
) -> list[dict] | None:
    """被动注入用的模组原文摘录：query=当前场景标题+玩家本轮最新输入，top-3。

    未建索引（rag_status != ready）、开场（无事件）或检索失败一律返回 None——
    build_kp_context 收到 None 时行为与无此特性完全一致（fail-open，不阻塞跑团）。
    """
    if getattr(module, "rag_status", "") != "ready" or not events:
        return None
    sid = scene_id or game_session.current_scene_id
    query = " ".join(
        p for p in (
            _scene_title(module, sid),
            _latest_player_input(events, party_char_ids),
        ) if p
    ).strip()
    if not query:
        return None
    try:
        return module_rag_service.retrieve(db, module.id, query, k=3, scene_id=sid) or None
    except Exception:  # noqa: BLE001 — 检索失败不得阻塞生成主流程
        logger.exception("模组原文检索失败（已降级）：module=%s", module.id)
        return None


# plan.turn_kind → 规则书被动检索 query（规则术语导向）。roleplay/mixed 不注入：
# 无明确规则情境，检索命中噪声大且白耗 token。
_RULE_QUERY_BY_TURN_KIND = {
    "combat": "战斗 轮次 伤害 护甲",
    "investigate": "线索 检定 困难等级",
    "knowledge": "线索 检定 困难等级",
    "social": "社交 话术 取悦 恐吓 对抗",
    "move": "追逐 攀爬 跳跃",
}
# 疯狂/理智情境优先于 turn_kind：本轮计划或最近事件涉及理智损失时改查疯狂规则。
_SAN_RULE_QUERY = "疯狂 症状 恐惧"


def _plan_involves_san(plan: turn_planner.TurnPlan, events: list) -> bool:
    """本轮是否处于理智/疯狂情境：plan 的检定涉及理智，或最近事件刚发生过理智结算。"""
    skill = plan.check.skill or ""
    if "理智" in skill or "SAN" in skill.upper():
        return True
    for ev in (events or [])[-6:]:
        content = getattr(ev, "content", "") or ""
        if "理智检定" in content or "SAN" in content:
            return True
    return False


def _rule_excerpts_for_context(
    db: Session,
    module: Module,
    plan: turn_planner.TurnPlan | None,
    events: list,
) -> list[dict] | None:
    """被动注入用的规则书要点：按本轮 plan.turn_kind 组规则术语 query，检索 top-2。

    镜像 ``_module_excerpts_for_context`` 的 fail-open 模式：无 plan、开场（无事件）、
    turn_kind 无对应规则情境、该规则系统未挂规则书、或检索失败，一律返回 None——
    build_kp_context 收到 None 时行为与无此特性完全一致。
    query 用固定规则术语而非玩家输入：规则书语料是条文术语，掺入剧情叙述文本反而
    稀释余弦命中（与模组原文检索相反——那边语料本身就是叙事文本，才拼玩家输入）。
    """
    if plan is None or not events:
        return None
    query = _RULE_QUERY_BY_TURN_KIND.get(plan.turn_kind)
    if _plan_involves_san(plan, events):
        query = _SAN_RULE_QUERY
    if not query:
        return None
    try:
        if not rulebook_service.has_rulebook(db, module.rule_system):
            return None
        return rulebook_service.retrieve(db, query, module.rule_system, k=2) or None
    except Exception:  # noqa: BLE001 — 检索失败不得阻塞生成主流程
        logger.exception("规则书被动检索失败（已降级）：rule_system=%s", module.rule_system)
        return None


def _record_turn_usage(db: Session, game_session: GameSession, llm, events: list) -> None:
    """把主叙事那次调用的服务端真实 usage 落到 world_state.turn_usage，供「上下文占用」显示实测值。

    **必须在主叙事流结束后、validator/摘要等后续 complete 覆盖 llm.last_usage 之前**调用。
    fail-open：无 usage（Provider 不支持）或异常都静默跳过，徽标回落启发式估算。
    """
    u = getattr(llm, "last_usage", None)
    if not isinstance(u, dict):
        return
    pt = u.get("prompt_tokens")
    if not isinstance(pt, int):
        return
    try:
        ws = dict(game_session.world_state or {})
        ws["turn_usage"] = {
            "prompt_tokens": pt,
            "completion_tokens": u.get("completion_tokens") or 0,
            "total_tokens": u.get("total_tokens") or 0,
            "at_seq": (events[-1].sequence_num if events else 0) or 0,
        }
        game_session.world_state = ws
        db.commit()
    except Exception:
        logger.exception("落库回合 usage 失败（忽略）")


async def _run_generation(
    db: Session,
    session_id: str,
    game_session: GameSession,
    module: Module,
    player_char: Character,
    events: list,
    teammates: list[Character] | None = None,
    blind_results: list[str] | None = None,
    plan: turn_planner.TurnPlan | None = None,
) -> None:
    llm = get_llm()
    kp = KPAgent(llm)
    # 仅在非开场、且该规则系统已挂载规则书时，向 KP 广告 [RULE_LOOKUP] 能力
    rules_enabled = bool(events) and rulebook_service.has_rulebook(db, module.rule_system)
    # 仅在非开场、且模组原文索引就绪时，向 KP 广告 [MODULE_LOOKUP] 能力（镜像规则书模式）
    module_rag_enabled = bool(events) and getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (teammates or [])}
    matcher_npcs = _matcher_npcs(module, teammates, game_session)

    # 回合裁定计划：主链路（run_chat_generation）已在队友回合之前先跑好 plan 并记过线索台账，
    # 通过 plan 参数传入 → 此处不重复调用。其他入口（run_travel_generation / _run_kp_turn 尾部）
    # 不传 plan → 这里现跑并记账（钩子 a），行为与前移前完全一致。开场（无事件）不跑。
    if plan is None and events:
        plan_messages = turn_planner.build_turn_plan_messages(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled,
        )
        plan = await turn_planner.run_turn_planner(llm, plan_messages)
        # 世界记忆钩子 a：本轮裁定要揭示线索 → 写入线索台账（纯确定性，零额外 LLM 调用）
        if plan is not None:
            _record_clue_ledger_from_plan(
                db, game_session, plan, events, player_char, teammates,
            )

    # 幕后推演 → validator 预筛：最近幕后事件文本挂进 plan.safety.do_not_reveal，
    # 防 KP 把「玩家不可见」的幕后动态直接复述进旁白（单场景与分头路径共用此 plan）。
    if plan is not None:
        _augment_plan_with_backstage(plan, events)

    # 分头行动：按各成员「真实所在场景」归并（玩家经大地图、队友经 travel 动作更新的确定性位置）。
    # 身处 ≥2 个场景即分头 → 逐场景生成叙事。不再靠 LLM 猜分组、也不因「打算去X」误判。
    scene_groups = _location_groups(game_session, module, player_char, teammates)

    # 本回合队友暗骰（心理学等）的真实结果 → 一条「仅 KP 可见」的上下文消息（不落库/不广播）。
    blind_message = _team_blind_message(blind_results)

    # 规则书要点被动注入：按本轮 plan.turn_kind 预取规则条文（与 [RULE_LOOKUP] 主动查互补）。
    # 规则片段不依赖场景，分头行动时各分组共用同一份检索结果（与模组摘录「分头也注入」对齐）。
    rule_excerpts = _rule_excerpts_for_context(db, module, plan, events)

    if len(scene_groups) >= 2:
        # 分头行动 v1 仍走旧正则路径（与 use_tool_calls 开关无关）：多分组编排与
        # loop 的 group 标签/指令归并交互留待下一步，先保证主路径可开关灰度。
        await _run_split_generation(
            db, session_id, game_session, module, player_char, events,
            teammates, kp, llm, rules_enabled, matcher_npcs, scene_groups,
            plan=plan, blind_message=blind_message, rule_excerpts=rule_excerpts,
        )
        return

    messages = build_kp_context(
        game_session, module, player_char, events, teammates=teammates,
        rules_lookup_enabled=rules_enabled,
        module_excerpts=_module_excerpts_for_context(
            db, module, game_session, events, party_ids,
        ),
        module_lookup_enabled=module_rag_enabled,
        rule_excerpts=rule_excerpts,
    )
    # 战斗结果摘要已注入本轮上下文 → 清除，避免下一轮重复注入（读一次）。
    if (game_session.world_state or {}).get("combat_result"):
        ws = dict(game_session.world_state)
        ws.pop("combat_result", None)
        game_session.world_state = ws
        db.commit()
    if plan is not None:
        messages.append(turn_planner.build_turn_plan_message(plan))
    if blind_message is not None:
        messages.append(blind_message)

    # 玩家党名单（玩家 + AI 队友）：供台词归属守卫用——KP 绝不能用气泡替他们说话。
    party_names = {player_char.name} | {t.name for t in (teammates or [])}
    result = ["", "", [], [], []]
    if _tool_loop_active(llm):
        # 新路径：agent loop（标准工具调用）。指令在 loop 内经执行器完成（含文本指令兜底），
        # 不再走 _process_commands；validator 终检/落库/记忆钩子与旧路径共用（见下方）。
        messages.append(kp_tools.tool_mode_message())
        exclude = set()
        if not rules_enabled:
            exclude.add("rule_lookup")   # 未挂规则书：不提供该工具（镜像旧路径不广告）
        if not module_rag_enabled:
            exclude.add("module_lookup")  # 原文索引未就绪：同上
        execute = _build_kp_tool_executor(
            db, session_id, game_session, module, player_char, teammates, llm, result,
        )
        # 本轮基线序号 + 事件广播顺序清单：loop 内工具事件即时落库（较小序号），旁白收尾才落库，
        # 直接 resync 会顺序错乱；收尾按广播偏移把本轮（seq>base_seq）事件重排回交错顺序。
        base_seq = session_service.get_next_sequence_num(db, session_id) - 1
        event_order: list = []
        try:
            async for chunk in _run_kp_agent_loop(
                llm, messages, result, execute,
                tools=kp_tools.openai_tool_schemas(exclude=exclude),
                npcs=matcher_npcs, plan=plan, party_names=party_names,
                event_order=event_order,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            _persist_narration(db, session_id, result, event_order)
            _reorder_turn_events(db, session_id, event_order, base_seq)
            raise
        _record_turn_usage(db, game_session, llm, events)   # validator 前，趁 last_usage 仍是主叙事那次
        await _validate_and_patch_narration(llm, plan, result, event_order)
        _persist_narration(db, session_id, result, event_order)
        _reorder_turn_events(db, session_id, event_order, base_seq)
        # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
        )
    else:
        # 旧路径：单次流式生成 + 正则指令后处理（降级开关，行为不变）。
        # 取消（硬取消 task）或流式中途报错（如供应商抖动断流）时，已生成的叙事都要落库，
        # 否则客户端在收到 done 后 resync 会拉到空历史，造成「生成到一半聊天全部消失」。
        try:
            async for chunk in _stream_narration_filtered(
                kp, messages, result, npcs=matcher_npcs, party_names=party_names,
            ):
                room_hub.broadcast(session_id, chunk)
        except BaseException:
            # CancelledError(继承 BaseException) 与普通异常都先把已生成片段落库再上抛
            _persist_narration(db, session_id, result)
            raise
        _record_turn_usage(db, game_session, llm, events)   # validator 前，趁 last_usage 仍是主叙事那次
        await _validate_and_patch_narration(llm, plan, result)
        _persist_narration(db, session_id, result)
        # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
        )

        async for chunk in _process_commands(
            db, session_id, result[1], module, player_char, game_session, llm,
            teammates=teammates,
        ):
            room_hub.broadcast(session_id, chunk)

    await _finish_generation(db, session_id, llm)


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
    rule_excerpts: list[dict] | None = None,
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

    # 模组原文 RAG：与单场景路径同一门槛（索引就绪才广告 [MODULE_LOOKUP]/注入摘录）
    module_rag_enabled = bool(events) and getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (teammates or [])}

    combined: list[str] = []
    for grp in groups:
        label = grp["label"]
        members = "、".join(grp["members"])
        # 关键：以该组所在场景为锚构建上下文，否则每列都拿主角场景的 NPC/线索，
        # KP 只能把主角场景重复叙述一遍（两列讲同一件事）。
        messages = build_kp_context(
            game_session, module, player_char, events, teammates=teammates,
            rules_lookup_enabled=rules_enabled, viewer_scene_id=grp.get("scene_id"),
            module_excerpts=_module_excerpts_for_context(
                db, module, game_session, events, party_ids,
                scene_id=grp.get("scene_id"),
            ),
            module_lookup_enabled=module_rag_enabled,
            # 规则要点不依赖场景：各分组共用调用方预取的同一份（与模组摘录注入现状对齐）
            rule_excerpts=rule_excerpts,
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
        # 世界记忆钩子 c：本组 NPC 台词记入其互动史（听众＝该组成员，信息不跨组共享）
        _record_npc_say_memory(
            db, session_id, game_session, module, result[2], grp["members"],
        )
        combined.append(result[1])

    async for chunk in _process_commands(
        db, session_id, "\n".join(combined), module, player_char, game_session, llm,
        teammates=teammates,
    ):
        room_hub.broadcast(session_id, chunk)

    await _finish_generation(db, session_id, llm)


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
        + "此外九维属性也可申请检定（返回其中文名即可）：力量、体质、体型、敏捷、外貌、"
        "智力、意志、教育、幸运；「灵感」=智力、「知识」=教育。\n"
        "判断玩家是否在【主动要求做一次技能/属性检定】"
        "（如「我用心理学看看他说的真假」「我要过一个侦查检定」「掷个聆听」"
        "「我想过个教育检定回忆一下」「过一个力量把门撞开」）。\n"
        '是 → {"check": true, "skill": "技能名或属性中文名（尽量用上面列出的原名）"}\n'
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


def _team_guidance_from_plan(plan: turn_planner.TurnPlan | None) -> str:
    """从 plan.direction 派生给 AI 队友的软指引（目前只用 spotlight——把戏份让给冷场玩家）。

    只影响队友「优先照顾谁」，不授权队友替人决定/代言；无 plan 或无 spotlight 则为空串。
    """
    if plan is None or not plan.direction.spotlight:
        return ""
    return (
        "本轮请把互动机会和话头多留给："
        + "、".join(plan.direction.spotlight)
        + "（他们最近戏份偏少）。你仍然只能决定自己的言行，不得替他们做决定或代言。"
    )


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
        # 本轮暂存的「前往」动作（大地图前往加入本回合）已随推进转正 → 在建 KP 上下文前
        # 确定性同步该角色所在场景，随后 KP 会以正确位置叙述抵达见闻（无需再单独走一次生成）。
        commit_pending_travel(db, session_id, turn)
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

        # planner 前移：在队友回合之前先跑一次裁定计划，作为本回合的共享契约——队友据
        # plan.direction 派生的导演提示行动（如把话头递给冷场玩家），KP 叙事时再以队友实际
        # 行动 + plan 为准。plan 是「裁定意图」不是「剧本」，队友行动后语义不变；开场不跑。
        pre_events = session_service.get_session_events(db, session_id)
        plan = None
        if pre_events:
            rules_enabled = rulebook_service.has_rulebook(db, module.rule_system)
            plan_messages = turn_planner.build_turn_plan_messages(
                game_session, module, player_char, pre_events,
                teammates=party_others, rules_lookup_enabled=rules_enabled,
            )
            plan = await turn_planner.run_turn_planner(llm, plan_messages)
            # 世界记忆钩子 a：本轮裁定要揭示线索 → 写入线索台账（前移后在此统一记账）
            if plan is not None:
                _record_clue_ledger_from_plan(
                    db, game_session, plan, pre_events, player_char, party_others,
                )

        # 玩家输入后：先跑一轮 AI 队友自动响应（仅 AI 席、仅一轮、不自触发），再交 KP 收束。
        # 队友暗骰（心理学等）的真实结果收集到 team_blind，注入本回合 KP 上下文而不落库/广播。
        team_blind: list[str] = []
        if ai_teammates:
            async for chunk in _run_team_turn(
                db, session_id, game_session, module, player_char, ai_teammates, llm,
                blind_results=team_blind,
                team_guidance=_team_guidance_from_plan(plan),
            ):
                room_hub.broadcast(session_id, chunk)

        events = session_service.get_session_events(db, session_id)
        await _run_generation(
            db, session_id, game_session, module, player_char, events,
            teammates=party_others, blind_results=team_blind, plan=plan,
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
    module_rag_enabled = getattr(module, "rag_status", "") == "ready"
    party_ids = {player_char.id} | {t.id for t in (party_others or [])}
    messages = build_kp_context(
        game_session, module, player_char, events,
        teammates=party_others, rules_lookup_enabled=rules_enabled,
        module_excerpts=_module_excerpts_for_context(
            db, module, game_session, events, party_ids,
        ),
        module_lookup_enabled=module_rag_enabled,
    )
    messages.append({"role": "user", "content": user_prompt})

    kp = KPAgent(llm)
    res = ["", "", [], [], []]
    try:
        async for chunk in _stream_narration_filtered(
            kp, messages, res, npcs=_matcher_npcs(module, party_others, game_session),
        ):
            room_hub.broadcast(session_id, chunk)
    except asyncio.CancelledError:
        _persist_narration(db, session_id, res)
        raise
    _persist_narration(db, session_id, res)
    # 世界记忆钩子 c：本轮 NPC 台词记入其互动史（对全队说话）
    _record_npc_say_memory(
        db, session_id, game_session, module, res[2],
        [player_char.name] + [t.name for t in (party_others or [])],
    )

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

    await _finish_generation(db, session_id, llm)


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


async def run_combat_aftermath_generation(session_id: str) -> None:
    """战斗/追逐结束后**主动**生成余波叙述——无需玩家先开口。

    复用既有「combat_result 折回主 KP」通道：build_kp_context 会把结果摘要注入本轮上下文，
    KP 承接直接后果、交代在场者状态、把主动权交还调查员。读一次即清 combat_result，
    避免玩家下一次行动时 _run_generation 再注入一遍余波。无结果摘要 / 无 LLM 则安静收场。
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        game_session = db.get(GameSession, session_id)
        if not game_session or not (game_session.world_state or {}).get("combat_result"):
            room_hub.broadcast(session_id, _make_chunk("done"))
            return
        module = db.get(Module, game_session.module_id)
        player_char = db.get(Character, game_session.player_character_id)
        party_others = session_service.get_party_members(
            db, session_id, exclude_id=game_session.player_character_id,
        )
        await _run_kp_turn(
            db, session_id, game_session, module, player_char, party_others,
            COMBAT_AFTERMATH_PROMPT,
        )
        # 读一次即清：_run_kp_turn 走 build_kp_context 注入了 combat_result 但不清除
        # （只有 _run_generation 会清），这里补清，避免下一次玩家回合重复注入余波。
        db.refresh(game_session)
        if (game_session.world_state or {}).get("combat_result"):
            ws = dict(game_session.world_state)
            ws.pop("combat_result", None)
            game_session.world_state = ws
            db.commit()
    except asyncio.CancelledError:
        logger.info("战斗余波生成被取消: session=%s", session_id)
    except Exception:
        logger.exception("战斗余波生成失败: session=%s", session_id)
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
        bonus = int(check.get("bonus") or 0)
        penalty = int(check.get("penalty") or 0)
        char_data, disp_name, _is_npc, _cid = _resolve_check_actor(
            check.get("char_ref", ""), skill, player_char, party_others, module,
        )
        engine = get_engine(module.rule_system)
        result = engine.resolve_check(char_data, skill, difficulty, bonus=bonus, penalty=penalty)
        tier_cn = TIER_LABEL.get(result.tier, result.tier)

        dice_content = (
            f"{disp_name}｜{skill} 检定（{difficulty}）：{tier_cn}（{result.description}）"
        )
        dice_meta = {
            "skill": skill, "skill_value": result.skill_value, "roll": result.roll,
            "target": result.target, "outcome": result.outcome, "tier": result.tier,
            "actor": disp_name, "dice": _check_dice_detail(result),
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
    except Exception as e:
        logger.exception("开场生成失败: session=%s", session_id)
        # 落库系统提示（而非仅广播）：否则客户端收到 done 后 resync 会把它一并抹掉。
        # 能归类的错误给出可行动原因（如 401→检查 Key），否则回落通用文案。
        hint = _classify_llm_error(e)
        msg = (
            f"（开场生成失败：{hint}。修好后点「重试开场」即可。）"
            if hint else "（开场生成中断，请点「重试开场」或刷新。）"
        )
        _persist_error_notice(db, session_id, msg)
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


# 疯狂/失能状态严重度：疯狂不覆盖更严重的既有状态，永久疯狂也不被临时疯狂降级。
_STATUS_SEVERITY = {
    "active": 0, "major_wound": 1, "temporary_insanity": 2,
    "indefinite_insanity": 3, "unconscious": 4, "permanent_insanity": 5, "dead": 6,
}


def _apply_madness_status(
    db: Session, char: Character, new_san: int, went_insane: bool,
) -> str | None:
    """据 SAN 结果落疯狂状态（确定性）：SAN 归零→永久疯狂；一次损失≥当前SAN/5→临时疯狂。

    只在新状态比既有更严重时升级（不把昏迷/死亡/永久疯狂降级为临时疯狂）。返回落定的状态或 None。
    """
    if new_san <= 0:
        target = "permanent_insanity"
    elif went_insane:
        target = "temporary_insanity"
    else:
        return None
    cur = char.status or "active"
    if _STATUS_SEVERITY.get(target, 0) <= _STATUS_SEVERITY.get(cur, 0):
        return None  # 既有状态已同等或更严重，不降级
    char.status = target
    db.add(char)
    db.commit()
    return target


# ── 指令执行器（旧正则路径与 agent loop 共用的单一实现）─────────────────────
# 每个函数执行一条指令并返回「待广播的 chunks（事件已落库）」+ 路径各自需要的信息。
# 旧路径（_process_commands）按正则匹配后调用；loop 路径（_build_kp_tool_executor）
# 按工具调用分发——执行逻辑只此一份，不复制。


async def _exec_san_check(
    db: Session, session_id: str, game_session: GameSession, kv: dict,
    player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], list[str]]:
    """执行一条理智检定：目睹者各自结算（同一角色对同一恐怖源只检定一次）。

    返回 (chunks, 回灌 KP 的结果描述列表)。
    """
    from app.rules.coc.checks import san_check

    chunks: list[str] = []
    descs: list[str] = []
    success_loss = (kv.get("success_loss") or "0").strip()
    failure_loss = (kv.get("failure_loss") or "1d6").strip()
    source = (kv.get("source") or "").strip()
    targets = _resolve_san_targets(kv.get("chars"), player_char, teammates)

    # 同一角色对同一恐怖源只检定一次：用 world_state.san_checked 记 "source|char_id"。
    ws = dict(game_session.world_state or {})
    san_checked = set(ws.get("san_checked") or [])
    san_dirty = False

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
        # 疯狂状态落库（确定性，不依赖 KP 自觉）：SAN 归零→永久疯狂；一次损失≥当前SAN/5→临时疯狂。
        # 不覆盖更严重的既有状态（死亡/昏迷/永久疯狂不被降级）。
        madness = _apply_madness_status(db, tchar, result["new_san"], result["went_insane"])
        if madness == "permanent_insanity":
            dice_content += "\n永久疯狂！SAN 归零，调查员就此永远失常。"
        elif madness == "temporary_insanity":
            dice_content += "\n临时疯狂！（一次性损失 SAN ≥ 当前 SAN/5）"

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
        # SAN 检定明骰：先落 SAN 判定本身的 d100 明细（check），再落损失骰池（pool）。
        # 前端据 dice 播 SAN 判定动画，据 loss_dice 播损失骰动画。
        dice_meta["check_dice"] = _check_dice_detail(check)
        loss_roll = result.get("loss_roll")
        if loss_roll is not None:
            dice_meta["dice"] = _pool_dice_detail(loss_roll)
        else:
            # 固定损失（如成功 0）：无骰池，明细直给定值，前端不必播骰。
            dice_meta["dice"] = {
                "kind": "pool", "notation": "0", "dice": [],
                "modifier": result["san_loss"], "total": result["san_loss"],
            }
        ev = session_service.add_event(
            db, session_id, "dice", dice_content,
            actor_name="系统", metadata=dice_meta,
        )
        chunks.append(_make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id))
        descs.append(
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
    return chunks, descs


def _resolve_hp_target(
    target_str: str, player_char: Character, teammates: list[Character] | None,
) -> Character | None:
    """把 HP_CHANGE 的 target= 解析成玩家方角色：空/player/主角/主角名→主角；队友名→该队友。

    NPC 或匹配不到 → None（NPC 的 HP 本系统不逐一追踪，维持不结算）。
    """
    name = (target_str or "").strip()
    if not name or name.lower() == "player" or name in ("主角", "玩家", player_char.name):
        return player_char
    for t in (teammates or []):
        if t.name and (t.name == name or name in t.name or t.name in name):
            return t
    return None


async def _exec_hp_change(
    db: Session, session_id: str, player_char: Character,
    target_str: str, delta_str: str, reason: str,
    module: Module | None = None,
    teammates: list[Character] | None = None,
) -> list[str]:
    """执行一条 HP 变化结算（target=player/主角 或队友名；NPC/匹配不到则不结算）。返回 chunks。

    确定性规则钩子（CoC 7e，不依赖 KP 自觉）：单次伤害 ≥ 最大 HP 一半即重伤——
    落 major_wound 状态并**系统自动**过一次体质（CON）检定，失败则昏迷（unconscious）。
    昏迷判定是被动生理反应而非玩家主动行动，故不走「待玩家投骰」、直接自动掷。
    队友受伤同样结算（多人局队友也会重伤/昏迷）。
    """
    char = _resolve_hp_target(target_str, player_char, teammates)
    if char is None:
        return []
    try:
        delta = int(str(delta_str).strip())
    except ValueError:
        return []
    reason = (reason or "").strip()
    hp_data = char.system_data.get("hitPoints", {})
    old_hp = hp_data.get("current", 0)
    max_hp = hp_data.get("max", old_hp)
    new_hp = max(0, min(max_hp, old_hp + delta))

    _update_character_stat(db, char, "hitPoints.current", new_hp)

    chunks: list[str] = []
    if delta < 0:
        hp_content = f"{char.name} 受到 {abs(delta)} 点伤害（HP {old_hp} → {new_hp}）"
        if reason:
            hp_content += f"——{reason}"
        major_wound = abs(delta) >= max_hp // 2 and max_hp > 0
        if major_wound:
            hp_content += "\n重伤！"
        if new_hp <= 0:
            hp_content += "\n濒死！需急救/医学稳定，否则将持续流失生命"
    else:
        hp_content = f"{char.name} 恢复 {delta} 点生命（HP {old_hp} → {new_hp}）"
        if reason:
            hp_content += f"——{reason}"
        major_wound = False

    ev = session_service.add_event(
        db, session_id, "system", hp_content,
        actor_name="系统",
        metadata={"hp_change": delta, "old_hp": old_hp, "new_hp": new_hp, "actor": char.name},
    )
    chunks.append(_make_chunk("system", hp_content, event_id=ev.id))

    # 重伤（未至濒死）→ 状态落库 + 自动体质检定判昏迷。fail-open：检定异常不阻塞结算。
    if major_wound and new_hp > 0 and module is not None:
        try:
            char.status = "major_wound"
            db.add(char)
            db.commit()
            engine = get_engine(module.rule_system)
            cdata = {
                "base_attributes": char.base_attributes,
                "skills": char.skills,
                "system_data": char.system_data,
            }
            result = engine.resolve_check(cdata, "体质", "normal")
            con_content = (
                f"{char.name}｜重伤体质检定（判定是否昏迷）：{result.description}"
            )
            if result.outcome in ("failure", "fumble"):
                char.status = "unconscious"
                db.add(char)
                db.commit()
                con_content += f"\n{char.name} 眼前一黑，昏迷倒地！"
            dev = session_service.add_event(
                db, session_id, "dice", con_content,
                actor_name="系统", metadata={
                    "skill": "体质", "roll": result.roll, "target": result.target,
                    "outcome": result.outcome, "actor": char.name,
                    "major_wound_check": True, "dice": _check_dice_detail(result),
                },
            )
            chunks.append(_make_chunk("dice", con_content, event_id=dev.id))
        except Exception:
            logger.exception("重伤体质检定失败（忽略，不阻塞结算）: char=%s", char.id)
    return chunks


async def _exec_dice_check(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    kv: dict, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], list[str], bool]:
    """执行一条技能检定。返回 (chunks, 回灌 KP 的结果描述, 是否挂成「待玩家投骰」)。

    真人控制、非暗投 → 不自动掷，挂 pending 并广播检定提示（pending=True，本轮就此收束）；
    NPC 暗骰 / AI 队友 / 暗投 → 系统自动掷，结果回灌。
    """
    chunks: list[str] = []
    descs: list[str] = []
    skill_name = (kv.get("skill") or "").strip()
    if not skill_name:
        return chunks, descs, False
    difficulty = (kv.get("difficulty") or "normal").strip() or "normal"
    char_ref = (kv.get("char") or "").strip()
    blind = (kv.get("visibility") or "open").strip().lower() == "blind"
    # 心理学等技能一律强制暗投：即使 KP 写了 visibility=open 或没写，也不挂「待玩家投骰」、
    # 不广播达成等级——结果只回灌 KP，玩家永远看不到成败。
    if any(s in skill_name for s in ALWAYS_BLIND_SKILLS):
        blind = True
    source = (kv.get("source") or "").strip()
    bonus, penalty = _parse_bonus_penalty(kv)

    # 群检：公共/被动感知事件（一声响、一个可触发灵感的线索——在场人人都可能注意到），
    # char=在场/全体 或 chars=<名单> → 在场每个玩家角色各自检定。被动性质天然自动掷，
    # 不逐人挂「待玩家投骰」（否则每有环境声响就要每个真人各点一次投骰，极其累赘）。
    group_ref = (kv.get("chars") or "").strip()
    if char_ref in _ALL_TOKENS or group_ref:
        targets = _resolve_dice_group_targets(
            char_ref, group_ref, game_session, player_char, teammates,
        )
        for c in targets:
            cdata = {
                "base_attributes": c.base_attributes,
                "skills": c.skills,
                "system_data": c.system_data,
            }
            rc, rd = await _auto_roll_check(
                db, session_id, game_session, module, cdata, c.name, False,
                skill_name, difficulty, blind, source, bonus, penalty,
            )
            chunks += rc
            descs += rd
        return chunks, descs, False

    char_data, disp_name, is_npc, char_id = _resolve_check_actor(
        char_ref, skill_name, player_char, teammates, module,
    )

    # req 1/2：真人控制、且非暗投的检定 → 不自动掷，挂成「待玩家投骰」并给出提示；
    # NPC 暗骰 / AI 队友 / 暗投 仍由系统自动掷（无人点投骰，避免卡住）。
    if (
        not is_npc and not blind
        and session_service.is_human_controlled(db, session_id, char_id)
    ):
        # 去重：分头行动下同一 plan 被注入每个分组，多组常各自吐出同一条 [DICE_CHECK]，
        # 合并文本后逐条处理会重复挂 pending、弹出两张相同的投骰卡。已存在等价（同角色+技能+
        # 难度）待投检定则跳过——不重复挂、不再广播 check_request（仍返回 True 收束本轮）。
        if session_service.find_pending_check(db, session_id, char_id, skill_name, difficulty):
            return chunks, descs, True
        check_id = uuid.uuid4().hex
        pending = {
            "id": check_id, "skill": skill_name, "difficulty": difficulty,
            "char_ref": char_ref, "char_id": char_id, "actor_name": disp_name,
            "source": source, "bonus": bonus, "penalty": penalty,
        }
        session_service.add_pending_check(db, session_id, pending)
        prompt_text = _check_prompt_text(disp_name, skill_name, difficulty)
        meta = {"check_request": True, **pending}
        ev = session_service.add_event(
            db, session_id, "system", prompt_text, actor_name="系统", metadata=meta,
        )
        chunks.append(_make_chunk(
            "check_request", prompt_text, metadata=meta,
            event_id=ev.id, actor_id=char_id,
        ))
        return chunks, descs, True  # 等玩家 /roll，本轮不掷、不续写

    rc, rd = await _auto_roll_check(
        db, session_id, game_session, module, char_data, disp_name, is_npc,
        skill_name, difficulty, blind, source, bonus, penalty,
    )
    return chunks + rc, descs + rd, False


async def _auto_roll_check(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    char_data: dict, disp_name: str, is_npc: bool,
    skill_name: str, difficulty: str, blind: bool, source: str,
    bonus: int, penalty: int,
) -> tuple[list[str], list[str]]:
    """系统自动掷一次检定并落库（不挂 pending）。单人自动路径与群检各成员共用。

    返回 (chunks, 回灌 KP 的结果描述)。暗投不落 dice 明细（会反推成败）。
    """
    chunks: list[str] = []
    descs: list[str] = []
    engine = get_engine(module.rule_system)
    result = engine.resolve_check(char_data, skill_name, difficulty, bonus=bonus, penalty=penalty)
    tier_cn = TIER_LABEL.get(result.tier, result.tier)

    if blind:
        kind_word = "暗骰" if is_npc else "暗投"
        dice_content = f"{disp_name} 进行了一次{kind_word}·{skill_name}（结果仅 KP 可见）"
        dice_meta = {"skill": skill_name, "actor": disp_name, "blind": True}
        descs.append(
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
            "dice": _check_dice_detail(result),
        }
        descs.append(
            f"{disp_name} {skill_name}（{difficulty}），达成 {tier_cn}"
            + (f"（针对：{source}）" if source else "")
            + f"：{result.description}"
        )

    ev = session_service.add_event(
        db, session_id, "dice", dice_content,
        actor_name="系统", metadata=dice_meta,
    )
    chunks.append(_make_chunk("dice", dice_content, metadata=dice_meta, event_id=ev.id))

    # 世界记忆钩子 d：暗投（玩家/队友对 NPC 的心理学等）若能经 source= 确定性归属到
    # 唯一 NPC，记录其「被看穿/未被看穿」；NPC 自己的暗骰或归属不成立则跳过。
    if blind and not is_npc:
        target = _match_single_npc(module, source)
        if target:
            seen_through = result.outcome in (
                "critical_success", "hard_success", "success",
            )
            verdict = "看穿" if seen_through else "试探，但未被看穿"
            _apply_world_memory(
                db, game_session,
                lambda ws: world_memory.record_npc_interaction(
                    ws, target[0], ev.sequence_num,
                    f"被 {disp_name} 用{skill_name}{verdict}",
                ),
            )
    return chunks, descs


async def _exec_scene_change(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    ref: str, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], str | None]:
    """执行一次场景切换。返回 (chunks, 解析成功且发生切换的 scene_id 或 None)。"""
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
        return [_make_chunk("system", f"场景切换至：{_scene_name(module, sid)}")], sid
    if not sid:
        logger.warning("SCENE_CHANGE 无法解析场景引用：%r（保持当前场景）", ref)
    return [], None


def _exec_flag(
    db: Session, session_id: str, game_session: GameSession, flag: str, value: bool,
) -> list[str]:
    """置/清剧情标志，并刷新内存里的 world_state 使后续处理立即可见。返回 chunks。"""
    session_service.set_flag(db, session_id, flag, value)
    db.refresh(game_session)
    label = "剧情推进" if value else "剧情状态解除"
    return [_make_chunk("system", f"{label}：{flag}")]


async def _exec_handout(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    hid: str, player_char: Character, teammates: list[Character] | None,
) -> tuple[list[str], str]:
    """发放一份手书（幂等：同 id 只发一次；未知 id 静默跳过）。返回 (chunks, 结果说明)。"""
    handout = next(
        (
            h for h in (getattr(module, "handouts", None) or [])
            if isinstance(h, dict) and str(h.get("id") or "").strip() == hid
        ),
        None,
    )
    if handout is None:
        logger.warning("HANDOUT 指令引用了未知手书 id（跳过）：%r", hid)
        return [], f"未知手书 id：{hid}（只发可发放清单里列出的 id）。"
    db.refresh(game_session)
    if world_memory.handout_issued(game_session.world_state or {}, hid):
        return [], f"手书 {hid} 已发放过（每份只发一次），不再重复。"
    title = str(handout.get("title") or "").strip()
    meta = {
        "kind": "handout",
        "handout_id": hid,
        "title": title,
        "handout_kind": str(handout.get("kind") or "").strip(),
    }
    ev = session_service.add_event(
        db, session_id, "system", str(handout.get("content") or ""),
        actor_name="系统", metadata=meta,
    )
    chunks = [_make_chunk("system", ev.content, metadata=meta, event_id=ev.id)]
    # 世界记忆：记入 handouts_issued（幂等真源）+ 线索台账（status=known，kind=handout），
    # 已发放的手书经台账自然进入后续 KP 上下文、并从「可发放清单」里消失。
    present = [player_char.id] + [t.id for t in (teammates or [])]
    _apply_world_memory(
        db, game_session,
        lambda ws, _hid=hid, _title=title, _seq=ev.sequence_num: (
            world_memory.record_handout_issue(ws, _hid, _title, present, _seq)
        ),
    )
    return chunks, f"手书 {hid} 已发放（正文已由系统以卡片呈现给玩家，续写时不要复述正文）。"



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
    chunks = [_make_chunk(
        "dialogue", npc_response, actor_name=npc_name,
        event_id=ev.id, actor_id=npc_id,
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
    _state, chunks = await combat_service.start(
        db, session_id, party, enemies, human_ids, trigger,
        agent=CombatAgent(llm), scene_hint=scene_hint,
    )
    return chunks


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


def _rule_lookup_passages(db: Session, query: str, rule_system: str) -> str:
    """检索规则书原文并拼成回灌片段；检索不到给降级文案（fail-open）。"""
    hits = rulebook_service.retrieve(db, query, rule_system, k=3)
    if hits:
        return "\n\n".join(f"[第 {h['page']} 页] {h['text']}" for h in hits)
    return "（未在规则书中找到直接匹配的内容，请依据《裁定手册》与你的经验处理。）"


def _module_lookup_passages(
    db: Session, module: Module, game_session: GameSession, query: str,
) -> str:
    """检索模组原文并拼成回灌片段；检索不到/失败给降级文案（fail-open）。"""
    try:
        hits = module_rag_service.retrieve(
            db, module.id, query, k=3, scene_id=game_session.current_scene_id,
        )
    except Exception:  # noqa: BLE001 — 检索失败降级为无命中
        logger.exception("模组原文检索失败（已降级）：module=%s", module.id)
        hits = []
    if hits:
        return "\n\n".join(
            f"[片段 {i}] {h['text']}" for i, h in enumerate(hits, start=1)
        )
    return "（未在模组原文中找到直接匹配的内容，请依据结构化模组资料与你的经验续写。）"


# ── KP agent loop（tool use 新路径，use_tool_calls 开关控制）──────────────────

# 单轮 loop 的步数上限：超限注入「请直接收束」再生成一次收尾，防无限工具链。
MAX_TOOL_LOOP_STEPS = 6


def _tool_loop_active(llm) -> bool:
    """KP 生成是否走 agent loop（工具调用）新路径：配置开关 && Provider 支持工具。

    开关默认关闭（use_tool_calls=false），任何读取异常一律回退旧路径（fail-open）。
    """
    try:
        from app.api.ai_settings import load_active_profile
        profile = load_active_profile()
    except Exception:
        return False
    if not profile or not getattr(profile, "use_tool_calls", False):
        return False
    try:
        return bool(llm.supports_tools())
    except Exception:
        return False


def _merge_step_result(result: list, step: list) -> None:
    """把一步生成的产物合并进整轮聚合 result（对话/分组偏移按已累计旁白长度平移）。"""
    base = len(result[0])
    result[0] += step[0]
    result[1] += step[1]
    if len(result) > 2 and len(step) > 2:
        result[2].extend(step[2])
    if len(result) > 3 and len(step) > 3:
        result[3].extend((off + base, spk, txt) for off, spk, txt in step[3])
    if len(result) > 4 and len(step) > 4:
        result[4].extend((off + base, label) for off, label in step[4])


# 裸值容错的单参数指令（[SET_FLAG hint_x] 这类漏写键名的旧习惯）→ 对应参数键
_SOLO_ARG_KEY = {
    "set_flag": "flag", "clear_flag": "flag", "handout": "id",
    "scene_change": "scene_id", "rule_lookup": "query", "module_lookup": "query",
}

_TEXT_TAG_RE = re.compile(r"\[([A-Z_]{3,})(?:[:：\s]([^\]]*))?\]")


def _tool_call_from_text(step_text: str) -> ToolCall | None:
    """loop 兜底：模型没走工具、而是把指令写成了文本（手写 prompt 的旧习惯）——
    把第一条终止型指令解析成等价的合成 ToolCall，交给同一执行器处理。

    只认注册表里的终止型指令（GROUP/SAY 是文本标注不算动作）。
    参数解析与旧正则同款宽容：键值对优先，单参数指令允许裸值。
    """
    text = (step_text or "").replace("【", "[").replace("】", "]")
    for m in _TEXT_TAG_RE.finditer(text):
        tag, inner = m.group(1), (m.group(2) or "").strip()
        # SAY/GROUP 是文本标注、不是动作：内联 [SAY] 由台词过滤器直接抽成气泡，
        # 这里绝不能再合成一个 say() 工具调用，否则同一句台词会重复出气泡。
        if tag in ("SAY", "GROUP"):
            continue
        name = kp_tools.TAG_TO_TOOL.get(tag)
        if name is None:
            continue
        kv = _parse_tag_kv(inner)
        if not kv and inner:
            solo = _SOLO_ARG_KEY.get(name)
            if solo:
                kv = {solo: inner}
        return ToolCall(id=f"text_{uuid.uuid4().hex[:8]}", name=name, arguments=kv)
    return None


def _plan_check_call(plan: turn_planner.TurnPlan) -> ToolCall:
    """裁定轮兜底：按计划的 check 字段拼出确定性补掷的 dice_check 调用
    （与 turn_planner._check_directive 同一语义，等价 KPAgent 的补指令兜底）。"""
    check = plan.check
    args: dict = {"skill": check.skill or "侦查"}
    if check.difficulty:
        args["difficulty"] = check.difficulty
    if check.visibility and check.visibility != "open":
        args["visibility"] = check.visibility
    return ToolCall(id=f"fallback_{uuid.uuid4().hex[:8]}", name="dice_check", arguments=args)


def _build_kp_tool_executor(
    db: Session, session_id: str, game_session: GameSession, module: Module,
    player_char: Character, teammates: list[Character] | None, llm,
    result: list,
):
    """构建 loop 路径的工具执行器：把注册表工具名分发到上面的共用执行函数（不复制逻辑）。

    闭包内维护每轮查阅配额（rule_lookup 与 module_lookup 合计 MAX_RULE_LOOKUPS 次，
    超限返回拒绝文本——与旧路径 lookup_depth 语义一致）。未知工具名回「无此工具」结果、
    任何执行异常 fail-open 返回错误说明，绝不断流。
    """
    lookup_used = 0

    async def execute(call: ToolCall) -> kp_tools.ToolOutcome:
        nonlocal lookup_used
        name = call.name
        kv = {k: str(v).strip() for k, v in (call.arguments or {}).items() if v is not None}
        spec = kp_tools.TOOLS_BY_NAME.get(name)
        if spec is None:
            return kp_tools.ToolOutcome(
                f"无此工具：{name}。只可调用系统提供的工具；若无需工具，直接继续叙述。"
            )
        try:
            if name == "dice_check":
                if not kv.get("skill"):
                    return kp_tools.ToolOutcome(
                        "参数缺失：skill 为必填。请带上技能名重试，或直接继续叙述。"
                    )
                chunks, descs, pending = await _exec_dice_check(
                    db, session_id, game_session, module, kv, player_char, teammates,
                )
                if pending:
                    return kp_tools.ToolOutcome(
                        "已向该玩家发出检定请求，等待其亲自掷骰。本轮叙述就此收束，绝不预测结果。",
                        chunks=chunks, suspend=True,
                    )
                return kp_tools.ToolOutcome(
                    KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)),
                    chunks=chunks,
                )
            if name == "opposed_check":
                descs: list[str] = []
                chunks = [
                    c async for c in _resolve_opposed(
                        db, session_id, kv, get_engine(module.rule_system),
                        module, player_char, teammates, descs,
                    )
                ]
                if not descs:
                    return kp_tools.ToolOutcome(
                        "参数缺失：skill（或 a_skill/b_skill）为必填。", chunks=chunks,
                    )
                return kp_tools.ToolOutcome(
                    KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)),
                    chunks=chunks,
                )
            if name == "san_check":
                chunks, descs = await _exec_san_check(
                    db, session_id, game_session, kv, player_char, teammates,
                )
                if not descs:
                    return kp_tools.ToolOutcome(
                        "本次理智检定无需结算（目睹者均已对该恐怖源检定过）。", chunks=chunks,
                    )
                return kp_tools.ToolOutcome(
                    KP_DICE_CONTINUATION_PROMPT.format(dice_results="\n".join(descs)),
                    chunks=chunks,
                )
            if name in ("rule_lookup", "module_lookup"):
                if lookup_used >= MAX_RULE_LOOKUPS:
                    return kp_tools.ToolOutcome(
                        f"本轮查阅配额已用完（规则书与模组原文合计最多 {MAX_RULE_LOOKUPS} 次），"
                        "请依据既有资料直接续写，不要再查阅。"
                    )
                query = kv.get("query", "").strip()
                if not query:
                    return kp_tools.ToolOutcome("参数缺失：query 为必填。")
                lookup_used += 1
                if name == "rule_lookup":
                    passages = _rule_lookup_passages(db, query, module.rule_system)
                    return kp_tools.ToolOutcome(
                        KP_RULE_CONTINUATION_PROMPT.format(query=query, passages=passages),
                        chunks=[_make_chunk("system", "守秘人翻阅规则书……")],
                    )
                passages = _module_lookup_passages(db, module, game_session, query)
                return kp_tools.ToolOutcome(
                    KP_MODULE_CONTINUATION_PROMPT.format(query=query, passages=passages),
                    chunks=[_make_chunk("system", "守秘人翻阅模组手稿……")],
                )
            if name == "say":
                who = kv.get("who", "").strip()
                text = kv.get("text", "").strip().strip("“”\"「」『』")
                if not who or not text:
                    return kp_tools.ToolOutcome("参数缺失：who 与 text 均为必填。")
                # 守卫：绝不用 say() 替玩家/队友说话或行动（他们的台词由本人给出）。
                party = {player_char.name} | {t.name for t in (teammates or [])}
                if _is_party_speaker(who, party):
                    return kp_tools.ToolOutcome(
                        f"拒绝：{who} 是玩家或队友角色，你不能替他们说话或行动。"
                        "玩家与队友的言行只能由他们本人给出；你只叙述 NPC 与环境，"
                        "把选择权留给他们。"
                    )
                chunks = _exec_say(result, module, who, text)
                return kp_tools.ToolOutcome(
                    "台词已作为气泡展示给玩家（续写时不要复述这句话）。", chunks=chunks,
                )
            if name == "start_combat":
                chunks = await _exec_start_combat(
                    db, session_id, game_session, module, player_char, teammates, llm,
                    kv.get("enemies", ""), kv.get("trigger", ""),
                )
                return kp_tools.ToolOutcome(
                    "已切入结构化战斗轮，交由系统按先攻推进；本轮就此收束，战斗结束后系统会回灌结果摘要。",
                    chunks=chunks, suspend=True,
                )
            if name == "start_chase":
                chunks = _exec_start_chase(
                    db, session_id, module, player_char, kv.get("pursuer", ""), kv.get("trigger", ""),
                )
                return kp_tools.ToolOutcome(
                    "已切入追逐（抽象距离轨），交由系统逐轮推进；本轮就此收束，追逐结束后系统会回灌结果。",
                    chunks=chunks, suspend=True,
                )
            if name == "npc_act":
                npc_id = kv.get("npc_id", "").strip()
                trigger = kv.get("trigger", "").strip()
                if not npc_id or not trigger:
                    return kp_tools.ToolOutcome("参数缺失：npc_id 与 trigger 均为必填。")
                chunks, response = await _exec_npc_act(
                    db, session_id, game_session, module, llm, player_char, npc_id, trigger,
                )
                return kp_tools.ToolOutcome(
                    f"该 NPC 已行动/开口（台词已直接展示给玩家，续写时不要复述）：{response}",
                    chunks=chunks,
                )
            if name == "scene_change":
                chunks, sid = await _exec_scene_change(
                    db, session_id, game_session, module,
                    kv.get("scene_id", "").strip(), player_char, teammates,
                )
                if sid:
                    return kp_tools.ToolOutcome(
                        f"ok：场景已切换至 {_scene_name(module, sid)}", chunks=chunks,
                    )
                return kp_tools.ToolOutcome("场景引用无法解析或未变化（保持当前场景）。", chunks=chunks)
            if name in ("set_flag", "clear_flag"):
                flag = kv.get("flag", "").strip()
                if not flag:
                    return kp_tools.ToolOutcome("参数缺失：flag 为必填。")
                chunks = _exec_flag(db, session_id, game_session, flag, name == "set_flag")
                return kp_tools.ToolOutcome("ok", chunks=chunks)
            if name == "hp_change":
                chunks = await _exec_hp_change(
                    db, session_id, player_char,
                    kv.get("target", ""), kv.get("delta", ""), kv.get("reason", ""),
                    module=module, teammates=teammates,
                )
                if chunks:
                    return kp_tools.ToolOutcome("ok", chunks=chunks)
                return kp_tools.ToolOutcome("未结算（target 当前仅支持 player，且 delta 须为整数）。")
            if name == "handout":
                hid = kv.get("id", "").strip()
                if not hid:
                    return kp_tools.ToolOutcome("参数缺失：id 为必填。")
                chunks, note = await _exec_handout(
                    db, session_id, game_session, module, hid, player_char, teammates,
                )
                return kp_tools.ToolOutcome(note, chunks=chunks)
        except Exception:
            logger.exception("工具执行失败: %s session=%s", name, session_id)
            return kp_tools.ToolOutcome(
                f"工具 {name} 执行出错，请不要重试该工具，直接继续叙述。"
            )
        return kp_tools.ToolOutcome(
            f"工具 {name} 暂无 loop 行为（内部错误），请直接继续叙述。"
        )

    return execute


async def _run_kp_agent_loop(
    llm, messages: list[dict], result: list, execute_tool, *,
    tools: list[dict] | None = None,
    npcs: list[dict] | None = None,
    group_label: str | None = None,
    plan: turn_planner.TurnPlan | None = None,
    max_steps: int = MAX_TOOL_LOOP_STEPS,
    party_names: set[str] | None = None,
    event_order: list | None = None,
) -> AsyncIterator[str]:
    """KP agent loop：与 _stream_narration_filtered 并列的新路径（use_tool_calls 开启时用）。

    stream_chat 流式生成：文本增量过同一套台词过滤后实时广播；tool_call 到达 →
    执行器执行 → 结果作为 role="tool" 消息回注 → 继续循环。DICE_CHECK/RULE_LOOKUP/
    MODULE_LOOKUP 由此天然取代旧路径的「续写 prompt」模式。

    - 步数上限 max_steps（默认 6）：超限注入「请直接收束本轮叙述」再无工具生成一次收尾；
    - 裁定轮（plan.requires_check）：采样降温 _CHECK_TURN_TEMPERATURE，且若模型始终没
      发起 dice_check/opposed_check，则按计划确定性补掷（等价 KPAgent 的补指令兜底）；
    - 模型把指令写成文本时（手写 prompt 旧习惯）：解析成合成 ToolCall 走同一执行器，
      [MOVE] 内联标记照旧生效，[SAY]/[GROUP] 由文本过滤器照常处理；
    - 执行器返回 suspend（如已挂「待玩家投骰」）：本轮生成就此收束。
    产物写入 result（与旧路径同构）；validator 终检、落库、世界记忆钩子、幕后推演等
    收尾由调用方与旧路径共用——loop 只替换「生成 + 指令执行」段。
    """
    tools = tools if tools is not None else kp_tools.openai_tool_schemas()
    requires_check = bool(plan is not None and plan.requires_check)
    temperature = _CHECK_TURN_TEMPERATURE if requires_check else 0.85
    messages = list(messages)  # loop 会往里回注消息，不污染调用方的列表
    did_check = False
    natural_end = False

    for _step in range(max_steps):
        step = ["", "", [], [], []]
        tool_calls: list[ToolCall] = []

        async def _text_deltas(calls=tool_calls):
            async for delta in llm.stream_chat(messages, tools=tools, temperature=temperature):
                if delta.kind == "text" and delta.text:
                    yield delta.text
                elif delta.kind == "tool_call" and delta.tool_call is not None:
                    calls.append(delta.tool_call)

        try:
            async for chunk in _filter_narration_stream(
                _text_deltas(), step, npcs=npcs, group_label=group_label,
                guess_speakers=False,  # 对话走 say() 工具；旁白里的裸引号一律留旁白、不猜
                party_names=party_names,  # 内联 [SAY] 误代言玩家/队友也挡下
            ):
                yield chunk
        except BaseException:
            _merge_step_result(result, step)  # 断流也保住已生成片段（调用方负责落库）
            raise
        _merge_step_result(result, step)

        step_text = (step[1] or "").replace("【", "[").replace("】", "]")
        if not tool_calls:
            synthetic = _tool_call_from_text(step_text)
            if synthetic is not None:
                tool_calls = [synthetic]
        if not tool_calls and requires_check and not did_check:
            # 裁定轮兜底：既没调工具也没写指令 → 确定性补掷计划指定的检定
            tool_calls = [_plan_check_call(plan)]
        if not tool_calls:
            natural_end = True
            break

        messages.append({
            "role": "assistant",
            "content": step[1] or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        })
        suspended = False
        for tc in tool_calls:
            if tc.name in ("dice_check", "opposed_check"):
                did_check = True
            outcome = await execute_tool(tc)
            for chunk in outcome.chunks:
                # 记录该工具事件在广播时的旁白偏移，供收尾把它重排回旁白中的正确位置
                if event_order is not None:
                    _record_chunk_event(event_order, chunk, len(result[0]))
                yield chunk
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": outcome.result_text,
            })
            if outcome.suspend:
                suspended = True
        if suspended:
            return

    if not natural_end:
        # 步数超限：注入收束指令，最后一次不带工具生成收尾
        messages.append({
            "role": "system",
            "content": (
                "（工具调用步数已达上限）请不要再调用任何工具、也不要输出任何指令，"
                "直接收束本轮叙述：自然交代当前处境，停在等待玩家行动处。"
            ),
        })
        step = ["", "", [], [], []]

        async def _plain_deltas():
            async for delta in llm.stream_chat(messages, tools=None, temperature=temperature):
                if delta.kind == "text" and delta.text:
                    yield delta.text

        try:
            async for chunk in _filter_narration_stream(
                _plain_deltas(), step, npcs=npcs, group_label=group_label,
                guess_speakers=False, party_names=party_names,
            ):
                yield chunk
        except BaseException:
            _merge_step_result(result, step)
            raise
        _merge_step_result(result, step)


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

    # 模组原文查阅同为终止性指令，与规则书查阅走同一开关与配额（lookup_depth 合并计数）
    if allow_rule_lookup and lookup_depth < MAX_RULE_LOOKUPS:
        mlookup = MODULE_LOOKUP_RE.search(kp_text)
        if mlookup:
            async for chunk in _handle_module_lookup(
                db, session_id, mlookup.group(1).strip(), module, player_char,
                game_session, llm, teammates=teammates, lookup_depth=lookup_depth,
            ):
                yield chunk
            return

    dice_descriptions: list[str] = []

    for match in SAN_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        san_chunks, san_descs = await _exec_san_check(
            db, session_id, game_session, kv, player_char, teammates,
        )
        for chunk in san_chunks:
            yield chunk
        dice_descriptions.extend(san_descs)

    for match in HP_CHANGE_RE.finditer(kp_text):
        hp_chunks = await _exec_hp_change(
            db, session_id, player_char,
            match.group(1).strip(), match.group(2).strip(), match.group(3).strip(),
            module=module, teammates=teammates,
        )
        for chunk in hp_chunks:
            yield chunk

    engine = get_engine(module.rule_system)

    for match in DICE_CHECK_RE.finditer(kp_text):
        kv = _parse_tag_kv(match.group(1))
        dice_chunks, dice_descs, _pending = await _exec_dice_check(
            db, session_id, game_session, module, kv, player_char, teammates,
        )
        for chunk in dice_chunks:
            yield chunk
        dice_descriptions.extend(dice_descs)

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
                kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
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
        # 世界记忆钩子 c：续写里的 NPC 台词同样记入其互动史
        _record_npc_say_memory(
            db, session_id, game_session, module, cont_result[2],
            [player_char.name] + [t.name for t in (teammates or [])],
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
        scene_chunks, _sid = await _exec_scene_change(
            db, session_id, game_session, module, match.group(1).strip(),
            player_char, teammates,
        )
        for chunk in scene_chunks:
            yield chunk

    # 剧情状态推进：置/清标志后，刷新内存里的 game_session.world_state，使本次生成的后续
    # 处理（续写、NPC 行动）与下一轮上下文都能看到最新状态。
    for match in SET_FLAG_RE.finditer(kp_text):
        for chunk in _exec_flag(db, session_id, game_session, match.group(1).strip(), True):
            yield chunk
    for match in CLEAR_FLAG_RE.finditer(kp_text):
        for chunk in _exec_flag(db, session_id, game_session, match.group(1).strip(), False):
            yield chunk

    # 手书发放：[HANDOUT: id=xxx] → 把模组手书的原文落库为 system 事件并广播（handout 是
    # 给玩家看的实体文书，正常进聊天流，前端按 metadata.kind 渲染成信笺卡片）。
    # 幂等：同 id 只发放一次（重复发放静默跳过）；未知 id 静默跳过（只记日志，不出卡片）。
    for match in HANDOUT_RE.finditer(kp_text):
        inner = match.group(1).strip()
        hid = (_parse_tag_kv(inner).get("id") or inner).strip()
        handout_chunks, _note = await _exec_handout(
            db, session_id, game_session, module, hid, player_char, teammates,
        )
        for chunk in handout_chunks:
            yield chunk

    for match in NPC_ACT_RE.finditer(kp_text):
        npc_chunks, _resp = await _exec_npc_act(
            db, session_id, game_session, module, llm, player_char,
            match.group(1).strip(), match.group(2).strip(),
        )
        for chunk in npc_chunks:
            yield chunk


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

    passages = _rule_lookup_passages(db, query, module.rule_system)
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
            kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
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

    # 世界记忆钩子 c：续写里的 NPC 台词同样记入其互动史
    _record_npc_say_memory(
        db, session_id, game_session, module, cont_result[2],
        [player_char.name] + [t.name for t in (teammates or [])],
    )

    # 续写里可能含查完规则后发起的检定/场景切换等，照常处理（但禁止再次查阅）
    async for chunk in _process_commands(
        db, session_id, cont_result[1], module, player_char, game_session, llm,
        teammates=teammates, allow_rule_lookup=False, lookup_depth=lookup_depth + 1,
    ):
        yield chunk


async def _handle_module_lookup(
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
    """KP 发起 [MODULE_LOOKUP] 后：检索模组原文 → 回灌让 KP 据此续写。

    与 [RULE_LOOKUP] 同一套处理模式，且共享 lookup_depth 配额（合并计数）。
    透明提示一条 ephemeral system（不落库）；检索不到/失败时给降级文案让 KP
    按结构化模组资料续写（fail-open，不阻塞跑团）。
    """
    yield _make_chunk("system", "守秘人翻阅模组手稿……")

    passages = _module_lookup_passages(db, module, game_session, query)
    continuation = KP_MODULE_CONTINUATION_PROMPT.format(query=query, passages=passages)
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
            kp, messages, cont_result, npcs=_matcher_npcs(module, teammates, game_session),
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

    # 续写里可能含查完原文后发起的检定/场景切换等，照常处理（但禁止再次查阅）
    async for chunk in _process_commands(
        db, session_id, cont_result[1], module, player_char, game_session, llm,
        teammates=teammates, allow_rule_lookup=False, lookup_depth=lookup_depth + 1,
    ):
        yield chunk
