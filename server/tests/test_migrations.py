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

    # Handouts 迁移（20260703）：modules 表带 handouts JSON 列
    con = sqlite3.connect(db_file)
    try:
        module_cols = {r[1] for r in con.execute("PRAGMA table_info(modules)")}
    finally:
        con.close()
    assert "handouts" in module_cols
    # 幕后真相迁移（20260719）：modules 表带 truth TEXT 列
    assert "truth" in module_cols


def test_run_migrations_is_idempotent(tmp_path, monkeypatch):
    db_file = tmp_path / "again.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    database.run_migrations()
    database.run_migrations()  # 第二次为 no-op，不应抛错


def test_noop_migration_creates_no_backup(tmp_path, monkeypatch):
    """已是最新时 run_migrations 为 no-op，不应留下备份文件（避免每次启动都堆备份）。"""
    db_file = tmp_path / "noop.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    database.run_migrations()
    database.run_migrations()
    assert not list(tmp_path.glob("noop.db.bak-*"))


def test_migration_backs_up_before_upgrading(tmp_path, monkeypatch):
    """有待应用迁移时，升级前先自动备份整库；升级后库到达最新。"""
    from alembic import command

    db_file = tmp_path / "up.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    database.run_migrations()  # 建到最新
    # 回退一格，制造「有待应用迁移」的状态
    command.downgrade(database._alembic_config(), "-1")
    cur_before, head = database.migration_status()
    assert cur_before != head

    database.run_migrations()  # 应先备份再升级
    backups = list(tmp_path.glob("up.db.bak-*"))
    assert backups, "迁移前应生成备份"
    cur_after, head2 = database.migration_status()
    assert cur_after == head2  # 已升到最新


def test_downgrade_scenario_rejected(tmp_path, monkeypatch):
    """库版本不在代码已知迁移链内（旧程序打开新库）时，拒绝迁移而非带病运行。"""
    import sqlite3

    import pytest

    db_file = tmp_path / "future.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    database.run_migrations()
    # 伪造一个「未来版本号」写进 alembic_version，模拟旧程序遇到更新的库
    con = sqlite3.connect(db_file)
    try:
        con.execute("UPDATE alembic_version SET version_num = 'zzzz_future_rev'")
        con.commit()
    finally:
        con.close()
    with pytest.raises(RuntimeError, match="高于本程序已知"):
        database.run_migrations()
