"""回合上下文、世界记忆、叙事校验与 RAG 输入构建。"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.ai import turn_planner, turn_validator
from app.models.character import Character
from app.models.module import Module
from app.models.session import GameSession
from app.services import (
    module_rag_service,
    rag_stats,
    rulebook_service,
    session_service,
    world_memory,
)

logger = logging.getLogger(__name__)

BACKSTAGE_DO_NOT_REVEAL_MAX = 3

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


def record_clue_ledger_from_plan(
    db: Session,
    game_session: GameSession,
    plan: turn_planner.TurnPlan,
    events: list,
    player_char: Character,
    teammates: list[Character] | None,
    module: Module | None = None,
    on_first_clue: Callable[[Session, str, Module, str], None] | None = None,
) -> None:
    """世界记忆钩子 a：planner 裁定本轮揭示线索（reveal_level != none 且有 candidate）
    即写入台账（partial ← hint，known ← direct）。

    discovered_by 取「与主角同场景」的玩家角色——分头行动下另一队并不知情，信息不共享。
    ``module`` 给定时，对**首次进台账**的线索追加一张发现配图卡（增强件，缺省不出卡）。
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
    # 记账前先取台账快照：新旧对比才知道哪些线索是**本轮首次**触碰（配图卡只出一次）
    before = set((game_session.world_state or {}).get("clue_ledger") or {})
    _apply_world_memory(db, game_session, lambda ws: world_memory.record_clue_reveal(
        ws, policy.candidate_clue_ids, policy.reveal_level, present, seq,
        note=policy.notes,
    ))
    if module is not None:
        for cid in policy.candidate_clue_ids:
            cid = str(cid or "").strip()
            if cid and cid not in before:
                if on_first_clue:
                    on_first_clue(db, game_session.id, module, cid)


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


def _recent_seen_text(events: list | None, limit: int = 6) -> str:
    """近期玩家已可感知的内容（最近几条旁白/台词/骰点结果）——喂校验器，让它别把已经在明面上的
    东西当泄露。只取玩家可见事件的正文，截断防过长。"""
    if not events:
        return ""
    seen: list[str] = []
    for ev in reversed(events):
        if getattr(ev, "event_type", None) in ("narration", "dialogue", "dice"):
            txt = (getattr(ev, "content", "") or "").strip()
            if txt:
                seen.append(txt[:200])
        if len(seen) >= limit:
            break
    return "\n".join(reversed(seen))


async def _validate_and_patch_narration(
    llm, plan: turn_planner.TurnPlan | None, result: list,
    event_order: list | None = None, seen_context: str = "",
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
    validation = await turn_validator.validate_turn_narration(llm, plan, result[0], seen_context)
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
        hits = module_rag_service.retrieve(db, module.id, query, k=3, scene_id=sid)
        _record_rag(db, game_session, kind="module", mode="passive", query=query, hits=hits)
        return hits or None
    except Exception:  # noqa: BLE001 — 检索失败不得阻塞生成主流程
        logger.exception("模组原文检索失败（已降级）：module=%s", module.id)
        return None


# plan.turn_kind → 规则书被动检索 query（规则术语导向）。roleplay/mixed 无映射
# （无明确规则情境），此时 _rule_query 会退到动作关键词 / 玩家原话兜底，仍然会查。
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


# 玩家动作里的规则触发词 → 规则书检索术语：让被动注入按**当前具体情境**取条文，
# 而非每种 turn_kind 一句死词（潜行/擒抱/穿透/孤注一掷等各自命中各自的规则页）。
_ACTION_RULE_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("潜行", "躲", "隐匿", "藏身"), "潜行 隐匿 躲藏 对抗 侦查"),
    (("擒", "抓住", "扭打", "制服", "缠斗", "抱摔"), "擒抱 制服 扭打 对抗"),
    (("攀", "爬", "翻越"), "攀爬 敏捷 跌落"),
    (("跳",), "跳跃 敏捷 跌落"),
    (("追", "逃跑", "甩开"), "追逐 移动 距离"),
    (("开枪", "射击", "扣动扳机", "开火", "瞄准", "连发", "扫射", "点射"), "射击 火器 穿透 连发 伤害"),
    (("格斗", "拳", "殴", "近战", "挥砍", "劈"), "格斗 近战 伤害 对抗"),
    (("急救", "止血", "包扎"), "急救 生命值 恢复 濒死"),
    (("医学", "治疗", "缝合"), "医学 治疗 生命值 恢复"),
    (("说服", "劝说", "话术"), "话术 说服 意志 对抗"),
    (("威胁", "恐吓", "逼问"), "恐吓 意志 对抗"),
    (("取悦", "讨好", "谄媚"), "取悦 社交 对抗"),
    (("燃烧", "点燃", "纵火", "汽油", "莫洛托夫", "火把"), "燃烧 火焰 每轮伤害"),
    (("中毒", "毒"), "中毒 体质 抗性"),
    (("孤注一掷", "豁出去", "拼了"), "孤注一掷 重掷 后果"),
    (("濒死", "重伤", "昏迷", "流血", "垂死"), "重伤 濒死 体质检定 死亡"),
    (("护甲", "防弹", "盔甲", "钢板"), "护甲 伤害 减免"),
    (("花幸运", "消耗幸运", "拼运气"), "幸运 消耗 补足"),
    (("发疯", "崩溃", "疯狂"), "疯狂 症状 恐惧"),
]


def _rule_keywords_from_events(events: list) -> str:
    """从最近几条玩家动作/发言里抽规则相关术语——只取规则显著的关键词，不整段掺叙事文本
    （规则语料是条文术语，掺叙事会稀释余弦命中）。planner 缺失或情境补强时用。"""
    text = " ".join(
        (getattr(e, "content", "") or "")
        for e in (events or [])[-4:]
        if getattr(e, "event_type", None) in ("action", "dialogue")
    )
    if not text:
        return ""
    hit = [terms for keys, terms in _ACTION_RULE_HINTS if any(k in text for k in keys)]
    return " ".join(hit)


def _san_context(plan: turn_planner.TurnPlan | None, events: list) -> bool:
    """本轮是否处于理智/疯狂情境（plan 可能为 None，故不复用 _plan_involves_san 的直接取值）。"""
    if plan is not None and _plan_involves_san(plan, events):
        return True
    for ev in (events or [])[-6:]:
        c = getattr(ev, "content", "") or ""
        if "理智检定" in c or "SAN" in c:
            return True
    return False


def _recent_player_text(events: list, limit_chars: int = 120) -> str:
    """玩家最近一条动作/发言的原文（截断）——术语词表没命中时的检索 query 兜底。

    bge 的 query 侧本就面向自然语言（embed_query 自带检索指令前缀），玩家原话直查
    好过不查：词表只该决定「查什么更准」，不该决定「查不查」。"""
    for ev in reversed(events or []):
        if getattr(ev, "event_type", None) in ("action", "dialogue"):
            text = (getattr(ev, "content", "") or "").strip()
            if text:
                return text[:limit_chars]
    return ""


def _rule_query(plan: turn_planner.TurnPlan | None, events: list) -> str | None:
    """KP 上下文用的规则检索 query，按优先级：

    1. planner 显式点名要查的规则（plan.rule_query）——裁定器最清楚本轮拿不准哪条；
    2. SAN 情境固定查疯狂规则；
    3. planner 的**具体技能** + turn_kind 术语 + 玩家动作关键词组合去重；
    4. 都组不出来时兜底用玩家最近发言原文——保证有玩家行动的回合**每轮必查**，
       不再因 turn_kind=roleplay/mixed 或词表未命中而整轮跳过（此前「查询长期偏少」的主因）。
    """
    if plan is not None and (explicit := (plan.rule_query or "").strip()):
        return explicit
    if _san_context(plan, events):
        return _SAN_RULE_QUERY
    parts: list[str] = []
    if plan is not None:
        skill = (plan.check.skill or "").strip()
        if skill:
            parts.append(skill)
        base = _RULE_QUERY_BY_TURN_KIND.get(plan.turn_kind)
        if base:
            parts.append(base)
    parts.append(_rule_keywords_from_events(events))
    seen: set[str] = set()
    toks = [t for t in " ".join(parts).split() if not (t in seen or seen.add(t))]
    return " ".join(toks) or _recent_player_text(events) or None


def _retrieve_rules(
    db: Session, module: Module, query: str, game_session: GameSession | None,
) -> list[dict] | None:
    """按 query 检索规则书 top-3 并记 RAG 台账。未挂规则书/检索失败一律 None（fail-open）。"""
    try:
        if not rulebook_service.has_rulebook(db, module.rule_system):
            return None
        hits = rulebook_service.retrieve(db, query, module.rule_system, k=3)
        _record_rag(db, game_session, kind="rule", mode="passive", query=query, hits=hits)
        return hits or None
    except Exception:  # noqa: BLE001 — 检索失败不得阻塞生成主流程
        logger.exception("规则书检索失败（已降级）：rule_system=%s", module.rule_system)
        return None


def _rule_excerpts_for_context(
    db: Session,
    module: Module,
    plan: turn_planner.TurnPlan | None,
    events: list,
    game_session: GameSession | None = None,
) -> list[dict] | None:
    """被动注入用的规则书要点（供 KP 上下文）：按当前**具体情境**组 query 检索 top-3。

    fail-open：开场（无事件）、组不出 query（本轮无任何玩家动作/发言）、未挂规则书、
    或检索失败一律返回 None——build_kp_context 收到 None 时行为与无此特性完全一致。
    """
    if not events:
        return None
    query = _rule_query(plan, events)
    if not query:
        return None
    return _retrieve_rules(db, module, query, game_session)


def _rule_excerpts_for_planner(
    db: Session, module: Module, events: list, game_session: GameSession | None = None,
) -> list[dict] | None:
    """给 planner 用的规则片段：planner 在 KP 之前跑、还没有 plan，故 query 只据玩家动作（+SAN 情境）取。
    让规则条文先进裁定环节，使难度/检定/奖惩骰/SAN 的判定更贴规则而非凭印象。
    词表未命中时兜底用玩家原话——有玩家行动就必查（同 _rule_query 的兜底逻辑）。"""
    if not events:
        return None
    query = _SAN_RULE_QUERY if _san_context(None, events) else _rule_keywords_from_events(events).strip()
    query = query or _recent_player_text(events)
    if not query:
        return None
    return _retrieve_rules(db, module, query, game_session)


def _record_rag(
    db: Session, game_session: GameSession | None, *,
    kind: str, mode: str, query: str, hits: list | None,
) -> None:
    """把一次 RAG 检索并入本局 world_state.rag_stats（供后台评估 RAG 用量/命中质量）。

    fail-open：无会话或异常都静默跳过，绝不影响生成主流程。空命中也记（看「查了没查到」比例）。
    """
    if game_session is None:
        return
    try:
        game_session.world_state = rag_stats.record(
            dict(game_session.world_state or {}),
            kind=kind, mode=mode, query=query, hits=hits or [],
        )
        db.commit()
    except Exception:
        logger.exception("落库 RAG 统计失败（忽略）")
        db.rollback()


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
