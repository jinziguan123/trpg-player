"""KP 叙事事件的清洗、交错持久化与重排记录。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.character import Character
from app.models.session import GameSession
from app.services import session_service

logger = logging.getLogger(__name__)

_FAKE_CHECK_RESULT_RE = re.compile(
    r"^[^\n]*?检定（(?:normal|hard|extreme|regular|常规|困难|极难)）\s*[:：]\s*"
    r"(?:大成功|极难成功|困难成功|普通成功|普通失败|大失败|成功|失败)[^\n]*\n?",
    re.M,
)

def persist_error_notice(db: Session, session_id: str, text: str) -> None:
    """把生成中断提示落库为 system 事件，保证在客户端 resync 后仍可见。"""
    try:
        # 上游 flush/commit 失败后 Session 处于 failed transaction 状态；补偿写入前必须回滚。
        db.rollback()
        session_service.add_event(db, session_id, "system", text, actor_name="系统")
    except Exception:
        logger.exception("落库生成中断提示失败: session=%s", session_id)

# 只由引号字符（±空白）组成的整行——KP 把闭引号写在台词/[SAY] 之外时留下的「孤立引号行」，
# 渲染成旁白里孤零零的一个 ” / “（用户报的「双引号被分到旁白中」）。落库前整行剥除。
_ORPHAN_QUOTE_LINE_RE = re.compile(r"(?m)^[ \t　]*[“”「」『』\"]+[ \t　]*(?:\n|$)")

# 台词残留兜底：模型（尤其 deepseek）漏调 say()、把「名字[（身份）]：「台词」」写进叙述文本
# （有时把名字写重一遍，如「京山人吉：\n京山人吉（乘务员）：“…”」）。只抽**显式署名**且带强信号
# （有身份标注 或 重复同名前缀）的台词——裸引号、招牌标识、被提及、玩家党名一概不碰。
_LEAKED_SAY_RE = re.compile(
    r"(?P<name>[一-龥·]{2,8})(?P<role>（[^）\n]{1,10}）)?[：:][ \t　]*"
    r"[“「『\"](?P<text>[^”」』\"\n]{1,200})[”」』\"]"
)


def _extract_leaked_dialogue(
    narration: str, marks: list | None, party_names: set[str] | None,
) -> tuple[str, list | None]:
    """把漏进旁白的显式署名 NPC 台词抽成对话 mark、从旁白删除（含吞掉紧邻其前的「同名：」重复前缀）。

    保守触发：仅当命中「名字（身份）：「台词」」有身份标注、或前面紧跟「同名：」重复前缀时才抽——
    二者都是「漏调 say()」的强信号；无信号的裸引号/招牌一律留旁白（不猜、不误抽）。玩家党名不抽。
    """
    party = set(party_names or ())
    hits: list[tuple[int, int, str, str]] = []   # (start, end, speaker, text)
    last_end = 0
    for m in _LEAKED_SAY_RE.finditer(narration):
        name = (m.group("name") or "").strip()
        if not name or name in party:
            continue
        s, e = m.start(), m.end()
        dup = re.search(re.escape(name) + r"[：:][ \t　]*[\r\n]*[ \t　]*$", narration[:s])
        if not (m.group("role") or dup):   # 无强信号 → 不抽（可能是招牌/标识/被提及）
            continue
        if dup:
            s = dup.start()
        s = max(s, last_end)               # 防与前一命中区间重叠
        last_end = e
        hits.append((s, e, name, (m.group("text") or "").strip()))
    if not hits:
        return narration, marks
    logger.info("台词残留兜底：从旁白抽出 %d 条显式署名台词（模型漏调 say()）", len(hits))
    removals = [(s, e) for s, e, _, _ in hits]

    def _shift(off: int) -> int:
        removed = 0
        for s, e in removals:
            if e <= off:
                removed += e - s
            elif s < off < e:
                removed += off - s
        return off - removed

    kept: list[str] = []
    inserted: list[tuple[int, str, str]] = []
    cursor = 0
    for s, e, name, text in hits:
        kept.append(narration[cursor:s])
        if text:
            inserted.append((_shift(s), name, text))
        cursor = e
    kept.append(narration[cursor:])
    new_narr = "".join(kept)
    old = [(_shift(o), spk, txt) for o, spk, txt in (marks or [])]
    return new_narr, sorted(old + inserted, key=lambda x: x[0])


def _session_party_names(db: Session, session_id: str) -> set[str]:
    """本局玩家 + AI 队友的角色名（台词兜底抽取时排除，绝不替玩家党发声）。"""
    names: set[str] = set()
    gs = db.get(GameSession, session_id)
    if not gs:
        return names
    if gs.player_character_id:
        pc = db.get(Character, gs.player_character_id)
        if pc and pc.name:
            names.add(pc.name)
    for mate in session_service.get_party_members(db, session_id):
        nm = getattr(mate, "name", None)
        if nm:
            names.add(nm)
    return names


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


def persist_narration(
    db: Session,
    session_id: str,
    result: list,
    event_order: list | None = None,
    *,
    attach_npc_portraits: Callable[[Session, str, list], None] | None = None,
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
    # 台词残留兜底：模型漏调 say()、把显式署名台词写进旁白 → 抽成对话气泡、清掉重复前缀。
    narration, marks = _extract_leaked_dialogue(narration, marks, _session_party_names(db, session_id))
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

    # 本次落库的 NPC 对话事件：收尾统一挂立绘（缓存秒挂/后台生成，fail-open 不影响落库）
    dialogue_evs: list = []

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
                    dialogue_evs.append(ev)
            pos = off
        _add_narr(narration[pos:], pos)
        if attach_npc_portraits:
            attach_npc_portraits(db, session_id, dialogue_evs)
        return

    # 回退：无交错信息
    _add_narr(narration, 0)
    for npc_name, dialogue_text in result[2]:
        ev = session_service.add_event(
            db, session_id, "dialogue", dialogue_text, actor_name=npc_name,
        )
        _record(ev, len(narration))
        dialogue_evs.append(ev)
    if attach_npc_portraits:
        attach_npc_portraits(db, session_id, dialogue_evs)


def record_chunk_event(event_order: list, chunk: str, offset: int) -> None:
    """把一条广播 chunk 对应的「已落库事件」记入重排清单：(此刻旁白长度作偏移, 事件 id)。
    只记带 id 的持久事件（骰子/检定请求/NPC 台词/HP 变化等 loop 内即时落库的展示事件）。"""
    try:
        data = json.loads(chunk[len("data: "):]) if chunk.startswith("data: ") else None
    except (ValueError, TypeError):
        return
    if data and data.get("id"):
        event_order.append((offset, data["id"]))
