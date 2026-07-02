"""从真实会话导出评测 fixture。

用法（在 server/ 目录下，读取本机 trpg.db）：
    python -m evals.snapshot --list                     # 列出会话
    python -m evals.snapshot <session_id> --show        # 看事件尾部，选切点
    python -m evals.snapshot <session_id> --turn 42 --name manor_check --tags kp_core,check
    python -m evals.snapshot <session_id> --turn 42 --with-plan   # 顺带跑一次 planner 预存计划

切点（--turn）应选在「玩家本轮输入的最后一条事件」上：重放时以截至该事件的历史
重新生成 KP 回合。不给 --turn 则取最后一条事件。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.database import SessionLocal
from app.models import Character, GameSession, Module
from app.services import rulebook_service, session_service

from evals.common import FIXTURES_DIR, row_to_dict, save_fixture


def _list_sessions() -> None:
    db = SessionLocal()
    try:
        sessions = db.query(GameSession).order_by(GameSession.created_at.desc()).all()
        if not sessions:
            print("库里没有会话。")
            return
        for s in sessions:
            module = db.get(Module, s.module_id)
            n_events = len(session_service.get_session_events(db, s.id, limit=0))
            print(f"{s.id}  [{s.status:<6}]  {module.title if module else '?'}  "
                  f"事件 {n_events} 条  场景 {s.current_scene_id or '-'}")
    finally:
        db.close()


def _show_events(session_id: str, tail: int) -> None:
    db = SessionLocal()
    try:
        events = session_service.get_session_events(db, session_id, limit=0)
        for e in events[-tail:]:
            content = (e.content or "").replace("\n", " ")[:80]
            print(f"seq={e.sequence_num:<5} [{e.event_type:<9}] "
                  f"{(e.actor_name or '系统'):<10} {content}")
    finally:
        db.close()


def _export(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        session = db.get(GameSession, args.session_id)
        if session is None:
            print(f"会话不存在: {args.session_id}")
            return 1
        module = db.get(Module, session.module_id)
        player_char = (
            db.get(Character, session.player_character_id)
            if session.player_character_id else None
        )
        if module is None or player_char is None:
            print("会话缺少模组或主角，无法导出。")
            return 1

        events = session_service.get_session_events(db, args.session_id, limit=0)
        if args.turn is not None:
            events = [e for e in events if (e.sequence_num or 0) <= args.turn]
        if not events and args.turn is not None:
            print(f"截至 seq={args.turn} 没有任何事件，检查 --turn 取值（--show 可预览）。")
            return 1
        turn = events[-1].sequence_num if events else 0

        teammates = session_service.get_party_members(
            db, args.session_id, exclude_id=session.player_character_id,
        )
        rules_enabled = bool(events) and rulebook_service.has_rulebook(
            db, module.rule_system,
        )

        plan_data = None
        if args.with_plan and events:
            from app.ai import turn_planner
            from app.ai.llm_factory import get_llm
            plan_messages = turn_planner.build_turn_plan_messages(
                session, module, player_char, events,
                teammates=teammates or None, rules_lookup_enabled=rules_enabled,
            )
            plan = asyncio.run(turn_planner.run_turn_planner(get_llm(), plan_messages))
            if plan is None:
                print("警告：planner 现跑失败，fixture 将不含预存计划（重放时现跑）。")
            else:
                plan_data = plan.model_dump()

        name = args.name or f"{args.session_id[:8]}_turn{turn}"
        path = FIXTURES_DIR / f"{name}.json"
        if path.exists() and not args.force:
            print(f"{path} 已存在，用 --force 覆盖或换 --name。")
            return 1

        payload = {
            "meta": {
                "name": name,
                "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
                "note": args.note or "",
                "source": {"session_id": args.session_id, "turn": turn},
            },
            "module": row_to_dict(module),
            "session": row_to_dict(session),
            "player_char": row_to_dict(player_char),
            "teammates": [row_to_dict(t) for t in teammates],
            "events": [row_to_dict(e) for e in events],
            "rules_lookup_enabled": rules_enabled,
            "plan": plan_data,
        }
        save_fixture(path, payload)
        print(f"已导出 {path}（事件 {len(events)} 条，截至 seq={turn}，"
              f"队友 {len(teammates)} 人）")
        print("末尾事件（确认切点是玩家输入）：")
        for e in events[-3:]:
            print(f"  seq={e.sequence_num} [{e.event_type}] {e.actor_name}: "
                  f"{(e.content or '')[:60]}")
        return 0
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="从真实会话导出评测 fixture")
    parser.add_argument("session_id", nargs="?", help="会话 id")
    parser.add_argument("--list", action="store_true", help="列出所有会话")
    parser.add_argument("--show", action="store_true", help="打印事件尾部帮助选切点")
    parser.add_argument("--tail", type=int, default=30, help="--show 显示的事件条数")
    parser.add_argument("--turn", type=int, help="截止事件 seq（含）；默认最后一条")
    parser.add_argument("--name", help="fixture 名（默认 会话前缀_turnN）")
    parser.add_argument("--tags", help="逗号分隔的 tag，如 kp_core,leak_risk")
    parser.add_argument("--note", help="备注：这个用例考察什么")
    parser.add_argument("--with-plan", action="store_true",
                        help="现跑一次 planner 预存计划（重放更稳，但导出时花一次调用）")
    parser.add_argument("--force", action="store_true", help="覆盖同名 fixture")
    args = parser.parse_args()

    if args.list:
        _list_sessions()
        return
    if not args.session_id:
        parser.error("需要 session_id（或用 --list 查看）")
    if args.show:
        _show_events(args.session_id, args.tail)
        return
    sys.exit(_export(args))


if __name__ == "__main__":
    main()
