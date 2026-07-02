"""启动自动迁移的回归：run_migrations 能把空库一路升到最新 schema。

顺带守住迁移链可用（任何一个迁移脚本坏掉都会让本测试失败）。
"""

import sqlite3

from app import database
from app.config import settings


def test_run_migrations_builds_full_schema(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.db"
    monkeypatch.setattr(settings, "db_path", db_file)

    database.run_migrations()

    con = sqlite3.connect(db_file)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        con.close()

    # 迁移链跑通的标志：alembic 版本表 + 本次新增的 RAG 两表 + 既有核心表都在
    assert "alembic_version" in tables
    assert "rulebooks" in tables
    assert "rule_chunks" in tables
    assert "module_chunks" in tables
    assert "modules" in tables
    assert "game_sessions" in tables


def test_run_migrations_is_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "again.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    database.run_migrations()
    database.run_migrations()  # 第二次为 no-op，不应抛错
