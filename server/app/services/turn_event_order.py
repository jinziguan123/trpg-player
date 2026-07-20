"""回合事件顺序整理。

该模块只负责把本回合已经落库的展示事件按广播偏移重新编号，不负责生成、广播或业务规则。
作为 chat_service 的第一批拆分边界，保留纯数据库编排输入输出，后续可以独立补事务测试。
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.event_log import EventLog


def reorder_turn_events(
    db: Session, session_id: str, event_order: list, base_seq: int
) -> None:
    """按广播顺序重排本轮展示事件的 sequence_num，避免唯一约束下的瞬时冲突。"""
    if not event_order:
        return

    # 稳定按偏移排序；同偏移保持捕获顺序（loop 内事件先于收尾旁白追加，≈广播先后）。
    # event_order 只记录带广播 id 的事件；把本轮其余事件按原 sequence 追加，避免唯一约束下
    # 出现「部分事件被重排、另一部分占据目标序号」的隐性冲突。
    order: list[str] = []
    seen: set[str] = set()
    for _off, event_id in sorted(event_order, key=lambda item: item[0]):
        if event_id not in seen:
            seen.add(event_id)
            order.append(event_id)

    candidates = (
        db.query(EventLog)
        .filter(
            EventLog.session_id == session_id,
            EventLog.sequence_num > base_seq,
        )
        .order_by(EventLog.sequence_num.asc(), EventLog.id.asc())
        .all()
    )
    by_id = {event.id: event for event in candidates}
    ordered = [by_id[event_id] for event_id in order if event_id in by_id]
    ordered_ids = {event.id for event in ordered}
    ordered.extend(event for event in candidates if event.id not in ordered_ids)
    if not ordered:
        return

    # 交换序号时直接写最终值会触发 UNIQUE(session_id, sequence_num) 的瞬时冲突。
    # 先把本批事件移到当前会话最小序号以下的临时区间，再写连续最终序号。
    # 不能只看 candidates：本轮新事件通常从 base_seq+1 开始，而历史事件仍占据更小序号。
    # 临时区间必须整体低于会话内全部事件，否则第一阶段搬移就会撞上历史唯一键。
    session_min = (
        db.query(func.min(EventLog.sequence_num))
        .filter(EventLog.session_id == session_id)
        .scalar()
    )
    temp_start = (session_min or 0) - len(ordered) - 1
    for offset, event in enumerate(ordered):
        event.sequence_num = temp_start + offset
    db.flush()

    for offset, event in enumerate(ordered, start=1):
        event.sequence_num = base_seq + offset
    db.commit()
